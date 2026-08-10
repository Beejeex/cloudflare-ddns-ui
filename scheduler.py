"""
scheduler.py

Responsibility: Sets up the APScheduler AsyncIOScheduler and registers the
DDNS background check job. Exposes start/stop/reschedule helpers.
Does NOT: contain DNS business logic, config reading, or HTTP calls directly
— those are delegated entirely to DnsService and its collaborators.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session

from cloudflare.cloudflare_client import CloudflareClient
from cloudflare.unifi_client import UnifiClient
from db.database import engine
from exceptions import IpFetchError
from log_cleanup import run_cleanup
from presenters import build_record_row
from repositories.config_repository import ConfigRepository
from repositories.history_repository import HistoryRepository
from repositories.record_config_repository import RecordConfigRepository
from repositories.stats_repository import StatsRepository
from services.dns_service import DnsService
from services.history_service import HistoryService
from services.ip_service import IpService
from services.log_service import LogService
from services.stats_service import StatsService
from services.unifi_service import UniFiService

if TYPE_CHECKING:
    from services.broadcast_service import BroadcastService

logger = logging.getLogger(__name__)

# Job ID used to identify the DDNS check job in APScheduler
_JOB_ID = "ddns_check"


# ---------------------------------------------------------------------------
# Scheduler job
# ---------------------------------------------------------------------------


async def _ddns_check_job(
    http_client: httpx.AsyncClient,
    unifi_http_client: httpx.AsyncClient,
    broadcaster: BroadcastService | None = None,
    app_state: Any = None,
) -> None:
    """
    APScheduler job: runs one DDNS check cycle and optional log cleanup.

    Opens a fresh DB session for each run so stats and logs are committed
    atomically. All business logic is delegated to DnsService — this
    function only wires up collaborators and triggers the cycle.

    After the Cloudflare cycle, runs a UniFi sync pass for every record
    whose RecordConfig has unifi_enabled=True, creating or updating the
    corresponding UniFi DNS policy.

    After the full cycle completes, publishes SSE events via BroadcastService
    when one is provided:
    - ``ip_updated``      — current public IP JSON
    - ``records_updated`` — records-table HTML fragment (stats-only; no live CF call)
    - ``log_appended``    — empty signal to trigger log panel refresh

    Args:
        http_client: The long-lived shared httpx.AsyncClient from app.state.
        unifi_http_client: The UniFi-specific client (verify=False) from app.state.
        broadcaster: Optional BroadcastService to push SSE events after the cycle.
        app_state: Optional FastAPI app.state object. When provided, IpService
            uses its shared ip_cache so the cycle and the SSE broadcast do not
            issue redundant upstream IP lookups.

    Returns:
        None
    """
    logger.debug("DDNS check job triggered.")

    with Session(engine) as session:
        config_repo = ConfigRepository(session)
        stats_repo = StatsRepository(session)
        log_service = LogService(session)

        config = config_repo.load()
        zones = config_repo.get_zones(config)
        records = config_repo.get_records(config)

        if not config.api_token:
            logger.warning("No API token configured — skipping DDNS check cycle.")
            return

        # Load per-record settings so the cycle respects static IPs and disabled flags
        record_configs = RecordConfigRepository(session).get_all(records)

        cloudflare_client = CloudflareClient(
            http_client=http_client,
            api_token=config.api_token,
            cache=getattr(app_state, "listing_cache", None) if app_state else None,
        )
        ip_service = IpService(http_client=http_client, app_state=app_state)
        stats_service = StatsService(stats_repo)
        history_service = HistoryService(HistoryRepository(session))
        dns_service = DnsService(
            cloudflare_client,
            ip_service,
            stats_service,
            log_service,
            history_service=history_service,
        )

        await dns_service.run_check_cycle(records, zones, record_configs=record_configs)

        # -----------------------------------------------------------------
        # UniFi DNS policy sync
        # -----------------------------------------------------------------
        # For every managed record:
        #   unifi_enabled=True  → create or update the UniFi DNS policy
        #   unifi_enabled=False → delete the policy if one exists
        # All policy reconciliation lives in UniFiService — the job only
        # wires up the client and delegates.
        if config.unifi_enabled and config.unifi_host and config.unifi_api_key and config.unifi_site_id:
            unifi_client = UnifiClient(
                http_client=unifi_http_client,
                api_key=config.unifi_api_key,
                host=config.unifi_host,
                cache=getattr(app_state, "listing_cache", None) if app_state else None,
            )
            unifi_service = UniFiService(unifi_client, log_service, stats_repo)
            await unifi_service.sync_policies(
                records=records,
                record_configs=record_configs,
                site_id=config.unifi_site_id,
                default_ip=config.unifi_default_ip,
                host=config.unifi_host,
            )

        # Run daily log cleanup at the end of each cycle if due
        run_cleanup(session, days_to_keep=config.log_retention_days)

    # -------------------------------------------------------------------------
    # SSE broadcasts — fire after the DB session is closed (data is committed)
    # -------------------------------------------------------------------------
    if broadcaster is not None:
        import asyncio  # noqa: PLC0415 — local import keeps startup fast
        from shared_templates import templates  # noqa: PLC0415

        # Publish ip_updated with the last successfully fetched IP.
        # NOTE: Passing app_state lets IpService reuse the shared ip_cache, so
        # the cycle's IP lookup is not duplicated by the broadcast.
        try:
            _ip_svc = IpService(http_client=http_client, app_state=app_state)
            _current_ip = await _ip_svc.get_public_ip()
            # NOTE: Plain text — HTMX sse-swap uses it as innerHTML directly
            broadcaster.publish("ip_updated", _current_ip)
        except IpFetchError as exc:
            logger.debug("Broadcaster: could not publish ip_updated: %s", exc)

        # Publish records_updated with a stats-based render (no extra CF calls).
        # This gives connected clients a quick UI update; the SSE on-connect
        # render provides the full live state for newly connected clients.
        try:
            with Session(engine) as _bcast_session:
                _bcast_config_repo = ConfigRepository(_bcast_session)
                _bcast_config = _bcast_config_repo.load()
                _bcast_records = _bcast_config_repo.get_records(_bcast_config)
                _bcast_stats = StatsRepository(_bcast_session).get_bulk(_bcast_records)
                _bcast_cfgs = RecordConfigRepository(_bcast_session).get_all(_bcast_records)
                _, _, _, _unifi_default_ip, _unifi_enabled = (
                    _bcast_config.unifi_host,
                    _bcast_config.unifi_api_key,
                    _bcast_config.unifi_site_id,
                    _bcast_config.unifi_default_ip,
                    _bcast_config.unifi_enabled,
                )
            rows = [
                build_record_row(r, stats=_bcast_stats.get(r), cfg=_bcast_cfgs.get(r))
                for r in _bcast_records
            ]
            _html = templates.get_template("partials/records_table.html").render(
                {"records": rows, "unifi_enabled": _unifi_enabled, "unifi_default_ip": _unifi_default_ip}
            )
            broadcaster.publish("records_updated", _html)
        except Exception as exc:
            logger.warning("Broadcaster: could not publish records_updated: %s", exc)

        # Signal log panel to refresh
        broadcaster.publish("log_appended", "{}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_scheduler(
    http_client: httpx.AsyncClient,
    unifi_http_client: httpx.AsyncClient,
    interval_seconds: int = 300,
    broadcaster: BroadcastService | None = None,
    app_state: Any = None,
) -> AsyncIOScheduler:
    """
    Creates and returns a configured AsyncIOScheduler with the DDNS check job.

    The job runs immediately on startup (next_run_time=now) and then at the
    configured interval.

    Args:
        http_client: The shared httpx.AsyncClient to pass into the job.
        unifi_http_client: The UniFi-specific client (verify=False) to pass into the job.
        interval_seconds: Seconds between DDNS check cycles (default 300).
        broadcaster: Optional BroadcastService for SSE push after each cycle.
        app_state: Optional FastAPI app.state to enable IP caching in the job.

    Returns:
        A configured but not yet started AsyncIOScheduler.
    """
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _ddns_check_job,
        trigger="interval",
        seconds=interval_seconds,
        id=_JOB_ID,
        kwargs={
            "http_client": http_client,
            "unifi_http_client": unifi_http_client,
            "broadcaster": broadcaster,
            "app_state": app_state,
        },
        # NOTE: next_run_time=now triggers the first check immediately on startup
        # rather than waiting a full interval before the first run.
        next_run_time=datetime.now(timezone.utc),
        max_instances=1,  # Prevent overlapping runs if a cycle takes too long
    )
    logger.info("DDNS check job scheduled — interval: %ds.", interval_seconds)
    return scheduler


def reschedule(scheduler: AsyncIOScheduler, http_client: httpx.AsyncClient, interval_seconds: int) -> None:
    """
    Changes the DDNS check job's interval without restarting the scheduler.

    Called by action routes when the user saves a new check interval via the UI.

    Args:
        scheduler: The running AsyncIOScheduler instance from app.state.
        http_client: The shared httpx.AsyncClient (passed to the rescheduled job).
        interval_seconds: New interval in seconds.

    Returns:
        None
    """
    scheduler.reschedule_job(
        _JOB_ID,
        trigger="interval",
        seconds=interval_seconds,
    )
    logger.info("DDNS check job rescheduled — new interval: %ds.", interval_seconds)


async def run_ddns_check_now(
    http_client: httpx.AsyncClient,
    unifi_http_client: httpx.AsyncClient,
    broadcaster: BroadcastService | None = None,
    app_state: Any = None,
) -> None:
    """
    Runs one DDNS check cycle immediately, outside the normal schedule.

    Intended for the manual "Sync Now" UI trigger. Delegates entirely to
    _ddns_check_job so behaviour is identical to a scheduled run.

    Args:
        http_client: The shared httpx.AsyncClient from app.state.
        unifi_http_client: The UniFi-specific client from app.state.
        broadcaster: Optional BroadcastService for SSE push after the cycle.
        app_state: Optional FastAPI app.state to enable IP caching in the job.

    Returns:
        None
    """
    logger.info("Manual sync triggered via UI.")
    await _ddns_check_job(
        http_client=http_client,
        unifi_http_client=unifi_http_client,
        broadcaster=broadcaster,
        app_state=app_state,
    )
