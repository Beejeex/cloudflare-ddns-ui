"""
routes/api_routes.py

Responsibility: JSON and HTMX partial API endpoints consumed by the frontend
for live data (log tail, IP status, records refresh, SSE event stream) and
manual actions such as triggering an immediate sync cycle.
Does NOT: render full pages or manage DB sessions directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from shared_templates import APP_VERSION, templates
from sse_starlette.sse import EventSourceResponse

from cloudflare.cloudflare_client import CloudflareClient
from cloudflare.unifi_client import UnifiClient
from dependencies import (
    get_broadcaster,
    get_config_service,
    get_dns_service,
    get_history_service,
    get_http_client,
    get_ip_service,
    get_log_service,
    get_record_config_repo,
    get_stats_repo,
    get_unifi_client,
    get_unifi_http_client,
)
from exceptions import DnsProviderError, IpFetchError, UnifiProviderError
from presenters import build_record_row
from repositories.record_config_repository import RecordConfigRepository
from repositories.stats_repository import StatsRepository
from scheduler import run_ddns_check_now
from services.broadcast_service import BroadcastService
from services.config_service import ConfigService
from services.dns_service import DnsService
from services.history_service import HistoryService
from services.ip_service import IpService
from services.log_service import LogService
from utils import to_local_policy_name, utcnow_naive

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# NOTE: Configurable via SSE_PING_INTERVAL env var so integration tests can
# set it to a short value (e.g. 0.1) and avoid a 25-second hang on teardown.
_SSE_PING_INTERVAL: float = float(os.getenv("SSE_PING_INTERVAL", "25.0"))


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------


@router.get("/events")
async def sse_events(
    request: Request,
    broadcaster: BroadcastService = Depends(get_broadcaster),
    config_service: ConfigService = Depends(get_config_service),
    dns_service: DnsService = Depends(get_dns_service),
    ip_service: IpService = Depends(get_ip_service),
    stats_repo: StatsRepository = Depends(get_stats_repo),
    record_config_repo: RecordConfigRepository = Depends(get_record_config_repo),
    unifi_client: UnifiClient = Depends(get_unifi_client),
) -> EventSourceResponse:
    """
    Server-Sent Events stream that pushes live IP and records updates to clients.

    On connect the client immediately receives the current public IP
    (``ip_updated``) and a rendered records-table fragment (``records_updated``)
    so there is no blank display period even after an SSE reconnect.

    Subsequent events are forwarded from the BroadcastService queue as they
    arrive.  A ``ping`` event is sent every 25 seconds to prevent proxy
    connection timeouts.

    Args:
        request: The incoming FastAPI request.
        broadcaster: Fan-out bus — provides the subscriber queue.
        config_service: Provides managed records and zone config.
        dns_service: Fetches live DNS state via fetch_zone_record_map().
        ip_service: Provides the current public IP (cache-aware).
        stats_repo: Bulk stats lookup for the initial render.
        record_config_repo: Per-record settings for the initial render.
        unifi_client: Provides UniFi DNS policy state.

    Returns:
        An EventSourceResponse that streams SSE events to the client.
    """
    async def _generator():
        q = broadcaster.subscribe()
        try:
            # --- On-connect: push current IP immediately ---
            current_ip = "Unavailable"
            try:
                current_ip = await ip_service.get_public_ip()
            except IpFetchError as exc:
                logger.warning("SSE on-connect: could not fetch public IP: %s", exc)
            # NOTE: Plain text so HTMX sse-swap can set it directly as innerHTML
            yield {"event": "ip_updated", "data": current_ip}

            # NOTE: records_updated is NOT sent on-connect because the dashboard
            # page is already rendered fresh by the template.  Sending it here
            # would immediately trigger a location.reload() loop in the unified
            # grid view.  The scheduler pushes records_updated after each sync cycle.

            # --- Stream: forward queue events until disconnect ---
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=_SSE_PING_INTERVAL)
                    yield msg
                except asyncio.TimeoutError:
                    # NOTE: Keep-alive ping prevents proxy connection timeouts.
                    yield {"event": "ping", "data": ""}
        finally:
            broadcaster.unsubscribe(q)

    return EventSourceResponse(_generator())


# ---------------------------------------------------------------------------
# Manual sync trigger
# ---------------------------------------------------------------------------


@router.post("/trigger-sync", response_class=HTMLResponse)
async def trigger_sync(request: Request) -> HTMLResponse:
    """
    Runs one full DDNS + UniFi sync cycle immediately on demand.

    Pulls the shared HTTP clients from app.state so the job uses the same
    connections as the scheduler. Returns an HTMX-friendly indicator that
    is swapped into the button area and triggers a page reload on completion.

    Args:
        request: The incoming FastAPI request (used to access app.state).

    Returns:
        An HTMLResponse confirming the sync was triggered.
    """
    await run_ddns_check_now(
        http_client=request.app.state.http_client,
        unifi_http_client=request.app.state.unifi_http_client,
        broadcaster=getattr(request.app.state, "broadcaster", None),
        app_state=request.app.state,
    )
    # Empty body — the HTMX after-request handler triggers location.reload()
    return HTMLResponse(content="", status_code=200)


@router.get("/logs/recent", response_class=HTMLResponse)
async def get_recent_logs(
    request: Request,
    level: str = Query(default=""),
    log_service: LogService = Depends(get_log_service),
) -> HTMLResponse:
    """
    Returns the recent log entries as an HTML fragment for HTMX polling.

    The dashboard polls this endpoint every N seconds (configured via the
    hx-trigger attribute on the log panel) and swaps in the result.

    Args:
        request: The incoming FastAPI request.
        level: Optional severity filter ("INFO", "WARNING", "ERROR"); empty = all.
        log_service: Provides recent log entries from the DB.

    Returns:
        An HTMLResponse containing the log-panel partial fragment.
    """
    if level:
        recent_logs = log_service.get_by_level(level, limit=100)
    else:
        recent_logs = log_service.get_recent(limit=100)
    return templates.TemplateResponse(
        request,
        "partials/log_panel.html",
        {"logs": recent_logs},
    )


@router.get("/logs/export", response_class=PlainTextResponse)
async def export_logs_csv(
    request: Request,
    log_service: LogService = Depends(get_log_service),
) -> PlainTextResponse:
    """
    Exports the recent log entries as a CSV download.

    Args:
        request: The incoming FastAPI request.
        log_service: Provides recent log entries from the DB.

    Returns:
        A PlainTextResponse containing CSV with a Content-Disposition header.
    """
    import csv
    import io

    entries = log_service.get_recent(limit=1000)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["timestamp", "level", "message"])
    for entry in entries:
        writer.writerow([
            entry.timestamp.isoformat() if entry.timestamp else "",
            entry.level,
            entry.message,
        ])
    filename = f"ddns-logs-{utcnow_naive().strftime('%Y%m%d')}.csv"
    return PlainTextResponse(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/current-ip", response_class=PlainTextResponse)
async def current_ip(
    request: Request,
    ip_service: IpService = Depends(get_ip_service),
) -> str:
    """
    Returns the host's current public IP as plain text for the navbar HTMX poll.

    Args:
        request: The incoming FastAPI request.
        ip_service: Cache-aware provider of the current public IP.

    Returns:
        The public IP address string, or "Unavailable" on failure.
    """
    try:
        return await ip_service.get_public_ip()
    except IpFetchError as exc:
        logger.warning("Could not fetch public IP for navbar: %s", exc)
        return "Unavailable"


@router.post("/unifi/sites", response_class=HTMLResponse)
async def get_unifi_sites(
    request: Request,
    unifi_host: str = Form(default="", alias="unifi_host"),
    unifi_api_key: str = Form(default="", alias="unifi_api_key"),
    http_client: httpx.AsyncClient = Depends(get_unifi_http_client),
) -> HTMLResponse:
    """
    Queries the UniFi controller for all available sites and returns an HTML
    partial so the settings page can auto-fill or show a picker for the Site ID.

    Accepts the host and api_key in the form body (never as query parameters,
    which proxies may log) so the user does not need to save settings first.

    Args:
        request: The incoming FastAPI request.
        unifi_host: UniFi controller host (IP or hostname).
        unifi_api_key: UniFi API key.
        http_client: Shared async client with verify=False.

    Returns:
        HTML partial rendered from partials/unifi_sites.html.
    """
    context: dict = {"sites": [], "error": None}
    if not unifi_host or not unifi_api_key:
        context["error"] = "Enter a host and API key first."
    else:
        client = UnifiClient(
            http_client=http_client,
            api_key=unifi_api_key,
            host=unifi_host,
            cache=getattr(request.app.state, "listing_cache", None),
        )
        try:
            context["sites"] = await client.list_sites()
        except UnifiProviderError as exc:
            logger.warning("UniFi site discovery failed: %s", exc)
            context["error"] = str(exc)
    return templates.TemplateResponse(
        request,
        "partials/unifi_sites.html",
        context,
    )


@router.get("/health/json")
async def health_json() -> dict:
    """
    Returns application health as a JSON response.

    Returns:
        A dict with a "status" key set to "ok".
    """
    return {"status": "ok"}


@router.post("/verify-token", response_class=HTMLResponse)
async def verify_token(
    request: Request,
    api_token: str = Form(...),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> HTMLResponse:
    """
    Verifies a Cloudflare API token and returns its zones as an HTML fragment.

    The token is sent in the form body (never as a query parameter, which
    proxies may log). On success the fragment lists the accessible zones as
    click-to-add buttons for the Settings page's Alpine zone array.

    Args:
        request: The incoming FastAPI request.
        api_token: The Cloudflare API token to verify.
        http_client: The shared httpx.AsyncClient.

    Returns:
        An HTMLResponse fragment: a success/error badge plus zone buttons.
    """
    client = CloudflareClient(
        http_client=http_client,
        api_token=api_token,
        cache=getattr(request.app.state, "listing_cache", None),
    )
    try:
        valid = await client.verify_token()
    except DnsProviderError as exc:
        logger.warning("Token verification failed: %s", exc)
        return HTMLResponse(
            f'<span class="badge" style="color:#dc2626;">&#9888; Verification failed: {exc}</span>'
        )
    if not valid:
        return HTMLResponse(
            '<span class="badge" style="color:#dc2626;">&#9888; Token is invalid or inactive.</span>'
        )

    try:
        zones = await client.list_zones()
    except DnsProviderError as exc:
        logger.warning("Zone listing failed: %s", exc)
        zones = []

    zone_buttons = "".join(
        f'<button type="button" class="btn btn-ghost btn-sm" style="margin:0.15rem;" '
        f'@click="addZone({json.dumps(z["name"])}, {json.dumps(z["id"])})">+ {z["name"]}</button>'
        for z in zones
    )
    zone_list = zone_buttons or (
        '<span style="font-size:0.8rem; color:#94a3b8;">No zones found for this token.</span>'
    )
    return HTMLResponse(
        f'<div>'
        f'<span class="badge" style="color:#16a34a;">&#10003; Token valid</span>'
        f'<div style="margin-top:0.4rem; font-size:0.8rem; color:#475569;">Zones found — click to add:</div>'
        f'<div style="margin-top:0.25rem;">{zone_list}</div>'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# Configuration backup / migration
# ---------------------------------------------------------------------------


@router.get("/export")
async def export_config(
    request: Request,
    config_service: ConfigService = Depends(get_config_service),
    record_config_repo: RecordConfigRepository = Depends(get_record_config_repo),
) -> PlainTextResponse:
    """
    Exports all configuration (settings, managed records, per-record config) as JSON.

    The response is a downloadable attachment for backup or migration purposes.

    Args:
        request: The incoming FastAPI request.
        config_service: Provides application configuration.
        record_config_repo: Provides per-record settings.

    Returns:
        A PlainTextResponse containing the JSON payload as a download.
    """
    config = await config_service.get_config()
    records = await config_service.get_managed_records()
    record_configs = record_config_repo.get_all(records)
    payload = {
        "version": APP_VERSION,
        "exported_at": utcnow_naive().isoformat(),
        "config": {
            "api_token": config.api_token,
            "zones": await config_service.get_zones(),
            "records": records,
            "refresh": config.refresh,
            "interval": config.interval,
            "log_retention_days": config.log_retention_days,
            "k8s_enabled": config.k8s_enabled,
            "unifi_host": config.unifi_host,
            "unifi_api_key": config.unifi_api_key,
            "unifi_site_id": config.unifi_site_id,
            "unifi_default_ip": config.unifi_default_ip,
            "unifi_enabled": config.unifi_enabled,
        },
        "record_configs": [
            {
                "record_name": rc.record_name,
                "cf_enabled": rc.cf_enabled,
                "ip_mode": rc.ip_mode,
                "static_ip": rc.static_ip,
                "unifi_enabled": rc.unifi_enabled,
                "unifi_static_ip": rc.unifi_static_ip,
                "unifi_local_enabled": rc.unifi_local_enabled,
                "unifi_local_static_ip": rc.unifi_local_static_ip,
            }
            for rc in record_configs.values()
        ],
    }
    filename = f"ddns-config-{utcnow_naive().strftime('%Y%m%d')}.json"
    return PlainTextResponse(
        content=json.dumps(payload, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import")
async def import_config(
    request: Request,
    config_service: ConfigService = Depends(get_config_service),
    record_config_repo: RecordConfigRepository = Depends(get_record_config_repo),
    log_service: LogService = Depends(get_log_service),
) -> dict:
    """
    Imports configuration from an exported JSON payload.

    Restores the main settings, the managed-records list, and per-record
    configs. The response reports how many records and zones were imported.

    Args:
        request: The incoming FastAPI request (JSON body).
        config_service: Provides application configuration.
        record_config_repo: Provides per-record settings.
        log_service: Writes a UI log entry on completion.

    Returns:
        A dict with "ok", "records", and "zones" keys.
    """
    body = await request.json()
    cfg_data = body.get("config") or {}

    zones: dict[str, str] = cfg_data.get("zones") or {}
    records: list[str] = cfg_data.get("records") or []
    await config_service.update_credentials(
        api_token=cfg_data.get("api_token", ""),
        zones=zones,
        refresh=int(cfg_data.get("refresh", 30)),
        interval=int(cfg_data.get("interval", 300)),
        log_retention_days=int(cfg_data.get("log_retention_days", 7)),
        k8s_enabled=bool(cfg_data.get("k8s_enabled", False)),
        unifi_host=cfg_data.get("unifi_host", ""),
        unifi_api_key=cfg_data.get("unifi_api_key", ""),
        unifi_site_id=cfg_data.get("unifi_site_id", ""),
        unifi_default_ip=cfg_data.get("unifi_default_ip", ""),
        unifi_enabled=bool(cfg_data.get("unifi_enabled", False)),
    )
    await config_service.replace_managed_records(records)

    for rc_data in body.get("record_configs") or []:
        rc = record_config_repo.get(rc_data["record_name"])
        rc.cf_enabled = bool(rc_data.get("cf_enabled", False))
        rc.ip_mode = rc_data.get("ip_mode", "dynamic")
        rc.static_ip = rc_data.get("static_ip", "")
        rc.unifi_enabled = bool(rc_data.get("unifi_enabled", False))
        rc.unifi_static_ip = rc_data.get("unifi_static_ip", "")
        rc.unifi_local_enabled = bool(rc_data.get("unifi_local_enabled", False))
        rc.unifi_local_static_ip = rc_data.get("unifi_local_static_ip", "")
        record_config_repo.save(rc)

    log_service.log(
        f"Imported configuration: {len(records)} record(s), {len(zones)} zone(s).",
        level="INFO",
    )
    return {"ok": True, "records": len(records), "zones": len(zones)}


@router.get("/next-check-in")
async def next_check_in(
    request: Request,
    config_service: ConfigService = Depends(get_config_service),
) -> dict:
    """
    Returns the seconds remaining until the next scheduled DDNS check.

    Reads the live next_run_time from APScheduler so the dashboard countdown
    stays accurate across page refreshes.

    Args:
        request: The incoming FastAPI request.
        config_service: Provides the configured DDNS check interval.

    Returns:
        A dict with "seconds" (int) and "interval" (int) keys.
    """
    from datetime import datetime, timezone

    interval = await config_service.get_check_interval()
    seconds_remaining = interval

    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        # NOTE: get_job()/next_run_time can fail if the scheduler has not
        # started yet or was shut down — fall back to the configured interval.
        try:
            job = scheduler.get_job("ddns_check")
            if job and job.next_run_time:
                delta = job.next_run_time - datetime.now(timezone.utc)
                seconds_remaining = max(0, int(delta.total_seconds()))
        except (AttributeError, ValueError) as exc:
            logger.debug("Could not read scheduler next_run_time: %s", exc)

    return {"seconds": seconds_remaining, "interval": interval}


# ---------------------------------------------------------------------------
# Records live refresh
# ---------------------------------------------------------------------------


@router.get("/records", response_class=HTMLResponse)
async def get_records(
    request: Request,
    config_service: ConfigService = Depends(get_config_service),
    dns_service: DnsService = Depends(get_dns_service),
    stats_repo: StatsRepository = Depends(get_stats_repo),
    unifi_client: UnifiClient = Depends(get_unifi_client),
    record_config_repo: RecordConfigRepository = Depends(get_record_config_repo),
    ip_service: IpService = Depends(get_ip_service),
) -> HTMLResponse:
    """
    Returns the managed records table as an HTMX fragment, plus OOB stat card updates.

    Triggered by the SSE `records_updated` event (or a manual sync).  Uses
    bulk zone + stats lookups to avoid N individual Cloudflare API calls.

    Args:
        request: The incoming FastAPI request.
        config_service: Provides configuration and managed records.
        dns_service: Fetches live DNS record state from Cloudflare.
        stats_repo: Provides per-record update/failure counters (bulk query).
        unifi_client: Fetches live UniFi DNS policies.
        record_config_repo: Provides per-record settings.
        ip_service: Cache-aware provider of the current public IP.

    Returns:
        An HTMLResponse with the records-table partial followed by
        hx-swap-oob elements that update the three dynamic stat cards.
    """
    config = await config_service.get_config()
    zones = await config_service.get_zones()
    managed_records = await config_service.get_managed_records()
    record_configs = record_config_repo.get_all(managed_records)

    # Fetch current public IP — fall back to empty string on failure.
    current_ip = ""
    try:
        current_ip = await ip_service.get_public_ip()
    except IpFetchError as exc:
        logger.warning("Could not fetch public IP for records refresh: %s", exc)

    _, _, unifi_site_id, unifi_default_ip, unifi_enabled = await config_service.get_unifi_config()
    unifi_policy_map: dict[str, object] = {}
    if unifi_enabled and unifi_client.is_configured() and unifi_site_id:
        try:
            policies = await unifi_client.list_records(unifi_site_id)
            unifi_policy_map = {p.name: p for p in policies}
        except UnifiProviderError as exc:
            logger.warning("UniFi policy fetch failed during records refresh: %s", exc)

    # Bulk DNS fetch (one call per zone) + bulk stats (one DB SELECT IN)
    zone_record_map: dict = {}
    if config.api_token and zones:
        try:
            zone_record_map = await dns_service.fetch_zone_record_map(managed_records, zones)
        except DnsProviderError as exc:
            logger.warning("records refresh: bulk CF lookup failed: %s", exc)

    stats_bulk = stats_repo.get_bulk(managed_records)

    record_data = []
    for record_name in managed_records:
        unifi_policy = unifi_policy_map.get(record_name)
        unifi_local_policy = unifi_policy_map.get(to_local_policy_name(record_name))
        record_data.append(build_record_row(
            record_name,
            dns_record=zone_record_map.get(record_name),
            current_ip=current_ip,
            stats=stats_bulk.get(record_name),
            cfg=record_configs.get(record_name),
            unifi_policy=unifi_policy,
            unifi_local_policy=unifi_local_policy,
            live=True,
        ))

    # Render records table partial as the main swap target.
    records_html = templates.get_template("partials/records_table.html").render(
        {
            "request": request,
            "records": record_data,
            "unifi_enabled": unifi_enabled,
            "unifi_default_ip": unifi_default_ip,
        }
    )

    # Append OOB element so HTMX updates the managed-count stat card without a full page reload.
    oob = f'<span id="stat-managed" hx-swap-oob="true">{len(record_data)}</span>'

    return HTMLResponse(content=records_html + oob)


# ---------------------------------------------------------------------------
# Per-record error log
# ---------------------------------------------------------------------------


@router.get("/logs/record/{record_name:path}", response_class=HTMLResponse)
async def get_record_error_logs(
    request: Request,
    record_name: str,
    log_service: LogService = Depends(get_log_service),
) -> HTMLResponse:
    """
    Returns recent ERROR/WARNING log entries that mention the given record as an HTML fragment.

    Used by the dashboard to populate the inline error panel when the
    user clicks on a failure counter.

    Args:
        request: The incoming FastAPI request.
        record_name: The FQDN to filter log entries by (path parameter).
        log_service: Provides log entry access.

    Returns:
        An HTMLResponse containing a small HTML fragment with the matching entries.
    """
    entries = log_service.get_errors_for_record(record_name, limit=20)
    return templates.TemplateResponse(
        request,
        "partials/record_error_log.html",
        {"entries": entries, "record_name": record_name},
    )


# ---------------------------------------------------------------------------
# Per-record IP change history
# ---------------------------------------------------------------------------


@router.get("/records/{record_name:path}/history", response_class=HTMLResponse)
async def get_record_history(
    request: Request,
    record_name: str,
    history_service: HistoryService = Depends(get_history_service),
) -> HTMLResponse:
    """
    Returns the recent IP change timeline for a record as an HTML fragment.

    Used by the dashboard's per-record config modal — the "IP change history"
    button loads this fragment on demand and swaps it into the modal.

    Args:
        request: The incoming FastAPI request.
        record_name: The FQDN to show history for (path parameter).
        history_service: Provides the per-record IP change timeline.

    Returns:
        An HTMLResponse containing partials/record_history.html.
    """
    entries = history_service.get_history(record_name, limit=30)
    return templates.TemplateResponse(
        request,
        "partials/record_history.html",
        {"entries": entries, "record_name": record_name},
    )