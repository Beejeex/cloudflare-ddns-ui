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

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session

from cloudflare.cloudflare_client import CloudflareClient
from cloudflare.unifi_client import UnifiClient
from db.database import engine
from exceptions import UnifiProviderError
from log_cleanup import run_cleanup
from repositories.config_repository import ConfigRepository
from repositories.record_config_repository import RecordConfigRepository
from repositories.stats_repository import StatsRepository
from services.dns_service import DnsService
from services.ip_service import IpService
from services.log_service import LogService
from services.stats_service import StatsService

logger = logging.getLogger(__name__)

# Job ID used to identify the DDNS check job in APScheduler
_JOB_ID = "ddns_check"


# ---------------------------------------------------------------------------
# Scheduler job
# ---------------------------------------------------------------------------


async def _ddns_check_job(http_client: httpx.AsyncClient, unifi_http_client: httpx.AsyncClient) -> None:
    """
    APScheduler job: runs one DDNS check cycle and optional log cleanup.

    Opens a fresh DB session for each run so stats and logs are committed
    atomically. All business logic is delegated to DnsService — this
    function only wires up collaborators and triggers the cycle.

    After the Cloudflare cycle, runs a UniFi sync pass for every record
    whose RecordConfig has unifi_enabled=True, creating or updating the
    corresponding UniFi DNS policy.

    Args:
        http_client: The long-lived shared httpx.AsyncClient from app.state.
        unifi_http_client: The UniFi-specific client (verify=False) from app.state.

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
        )
        ip_service = IpService(http_client=http_client)
        stats_service = StatsService(stats_repo)
        dns_service = DnsService(cloudflare_client, ip_service, stats_service, log_service)

        await dns_service.run_check_cycle(records, zones, record_configs=record_configs)

        # -----------------------------------------------------------------
        # UniFi DNS policy sync
        # -----------------------------------------------------------------
        # For every managed record:
        #   unifi_enabled=True  → create or update the UniFi DNS policy
        #   unifi_enabled=False → delete the policy if one exists
        if config.unifi_enabled and config.unifi_host and config.unifi_api_key and config.unifi_site_id:
            unifi_client = UnifiClient(
                http_client=unifi_http_client,
                api_key=config.unifi_api_key,
                host=config.unifi_host,
            )
            for record_name in records:
                cfg = record_configs.get(record_name)

                # --- Deletion pass: remove policy when user disables UniFi for this record ---
                if cfg is None or not cfg.unifi_enabled:
                    try:
                        existing = await unifi_client.get_record(config.unifi_site_id, record_name)
                        if existing is not None:
                            await unifi_client.delete_record(config.unifi_site_id, existing.id)
                            log_service.log(
                                f"UniFi: removed policy '{record_name}' (disabled by user).",
                                level="INFO",
                            )
                    except UnifiProviderError as exc:
                        log_service.log(
                            f"UniFi: failed to remove policy '{record_name}': {exc}",
                            level="ERROR",
                        )
                        logger.error("UniFi policy removal failed for %s: %s", record_name, exc)
                    continue

                # Determine target IP: per-record static → global default
                target_ip = (
                    cfg.unifi_static_ip.strip()
                    or config.unifi_default_ip.strip()
                )
                if not target_ip:
                    log_service.log(
                        f"UniFi sync skipped for '{record_name}': no IP configured.",
                        level="WARNING",
                    )
                    continue

                try:
                    existing = await unifi_client.get_record(config.unifi_site_id, record_name)
                    if existing is None:
                        await unifi_client.create_record(config.unifi_site_id, record_name, target_ip)
                        log_service.log(
                            f"UniFi: created policy '{record_name}' → {target_ip}",
                            level="INFO",
                        )
                    elif existing.content != target_ip:
                        await unifi_client.update_record(config.unifi_site_id, existing, target_ip)
                        log_service.log(
                            f"UniFi: updated policy '{record_name}' → {target_ip}",
                            level="INFO",
                        )
                    else:
                        logger.debug("UniFi policy '%s' already up to date (%s).", record_name, target_ip)
                except UnifiProviderError as exc:
                    log_service.log(
                        f"UniFi: failed to sync '{record_name}': {exc}",
                        level="ERROR",
                    )
                    logger.error("UniFi sync failed for %s: %s", record_name, exc)

        # Run daily log cleanup at the end of each cycle if due
        run_cleanup(session, days_to_keep=7)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_scheduler(
    http_client: httpx.AsyncClient,
    unifi_http_client: httpx.AsyncClient,
    interval_seconds: int = 300,
) -> AsyncIOScheduler:
    """
    Creates and returns a configured AsyncIOScheduler with the DDNS check job.

    The job runs immediately on startup (next_run_time=now) and then at the
    configured interval.

    Args:
        http_client: The shared httpx.AsyncClient to pass into the job.
        unifi_http_client: The UniFi-specific client (verify=False) to pass into the job.
        interval_seconds: Seconds between DDNS check cycles (default 300).

    Returns:
        A configured but not yet started AsyncIOScheduler.
    """
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _ddns_check_job,
        trigger="interval",
        seconds=interval_seconds,
        id=_JOB_ID,
        kwargs={"http_client": http_client, "unifi_http_client": unifi_http_client},
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
