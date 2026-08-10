"""
routes/action_routes.py

Responsibility: POST handlers that mutate state and return HTMX HTML fragments.
All responses are partial HTML — never redirects. HTMX swaps the fragment into
the page without a full reload.
Does NOT: render full pages, call DNS APIs directly, or manage DB sessions.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from shared_templates import templates

from dependencies import (
    get_broadcaster,
    get_config_service,
    get_dns_service,
    get_log_service,
    get_record_config_repo,
    get_stats_service,
    get_unifi_client,
)
from exceptions import DnsProviderError, UnifiProviderError
from presenters import build_record_row
from repositories.record_config_repository import RecordConfigRepository
from scheduler import reschedule
from services.broadcast_service import BroadcastService
from utils import mask_secret
from services.config_service import ConfigService
from services.dns_service import DnsService
from services.log_service import LogService
from services.stats_service import StatsService
from cloudflare.unifi_client import UnifiClient

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@router.post("/update-config", response_class=HTMLResponse)
async def update_config(
    request: Request,
    api_token: str = Form(...),
    zones: str = Form(...),
    refresh: int = Form(30),
    interval: int = Form(300),
    log_retention_days: int = Form(7),
    k8s_enabled: bool = Form(False),
    unifi_host: str = Form(""),
    unifi_api_key: str = Form(""),
    unifi_site_id: str = Form(""),
    unifi_default_ip: str = Form(""),
    unifi_enabled: bool = Form(False),
    config_service: ConfigService = Depends(get_config_service),
    log_service: LogService = Depends(get_log_service),
) -> HTMLResponse:
    """
    Saves new Cloudflare credentials, timing configuration, Kubernetes
    discovery toggle, and UniFi integration settings.

    HTMX swaps the returned fragment into #config-status on the page.
    After saving, publishes a ``records_updated`` SSE event so all open
    browser tabs reflect the config change without a manual refresh.

    Args:
        request: The incoming FastAPI request.
        api_token: Cloudflare API token from the config form.
        zones: JSON string of base-domain-to-zone-ID mapping.
        refresh: UI auto-refresh interval in seconds.
        interval: Background DDNS check interval in seconds.
        log_retention_days: How many days of activity logs to keep.
        k8s_enabled: Whether Kubernetes Ingress discovery is enabled.
        unifi_host: Hostname or IP of the local UniFi Network Application.
        unifi_api_key: UniFi API key with DNS write access.
        unifi_site_id: UniFi site UUID.
        unifi_enabled: Whether UniFi internal DNS management is enabled.
        config_service: Saves the new configuration.
        log_service: Writes a UI log entry on success.

    Returns:
        An HTMLResponse containing the config-status partial fragment.
    """
    try:
        zones_dict: dict[str, str] = json.loads(zones)
    except json.JSONDecodeError:
        zones_dict = {}
        logger.warning("update-config: invalid zones JSON received.")

    # Defense-in-depth: the settings form pre-fills masked placeholders instead
    # of the real secrets.  If the submitted value is still the masked form of
    # the stored value, keep the stored secret — never overwrite it with the
    # masked display string.
    stored_token = await config_service.get_api_token()
    if api_token == mask_secret(stored_token):
        api_token = stored_token

    _unifi_cfg = await config_service.get_unifi_config()
    if unifi_api_key == mask_secret(_unifi_cfg[1]):
        unifi_api_key = _unifi_cfg[1]

    await config_service.update_credentials(
        api_token=api_token,
        zones=zones_dict,
        refresh=refresh,
        interval=interval,
        log_retention_days=log_retention_days,
        k8s_enabled=k8s_enabled,
        unifi_host=unifi_host,
        unifi_api_key=unifi_api_key,
        unifi_site_id=unifi_site_id,
        unifi_default_ip=unifi_default_ip,
        unifi_enabled=unifi_enabled,
    )

    # Reschedule the background job with the new interval
    reschedule(
        scheduler=request.app.state.scheduler,
        http_client=request.app.state.http_client,
        interval_seconds=interval,
    )

    log_service.log("Cloudflare configuration updated.", level="INFO")

    # Publish a records_updated signal so all SSE clients refresh their view
    broadcaster = getattr(request.app.state, "broadcaster", None)
    if broadcaster is not None:
        broadcaster.publish("records_updated", "")
        broadcaster.publish("log_appended", "{}")

    return templates.TemplateResponse(
        request,
        "partials/config_status.html",
        {"success": True, "message": "Configuration saved."},
    )


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------


@router.post("/bulk-set-cf", response_class=HTMLResponse)
async def bulk_set_cf(
    request: Request,
    enabled: str = Form(default="true"),
    config_service: ConfigService = Depends(get_config_service),
    record_config_repo: RecordConfigRepository = Depends(get_record_config_repo),
    log_service: LogService = Depends(get_log_service),
    broadcaster: BroadcastService = Depends(get_broadcaster),
) -> HTMLResponse:
    """
    Enables or disables Cloudflare DDNS for every managed record at once.

    HTMX posts `enabled` ("true"/"false"); the action applies to all managed
    records in a single DB transaction, logs the outcome, and broadcasts a
    ``records_updated`` SSE event.  Returns an empty body — the dashboard's
    afterRequest handler reloads the page (see reloadPaths in dashboard.html).

    Args:
        request: The incoming FastAPI request.
        enabled: "true" to enable, anything else to disable.
        config_service: Provides the managed record list.
        record_config_repo: Applies the bulk flag to RecordConfig rows.
        log_service: Writes a bulk-action log entry.
        broadcaster: Pushes a records_updated SSE event to open tabs.

    Returns:
        An empty HTMLResponse (page reload is triggered client-side).
    """
    records = await config_service.get_managed_records()
    flag = enabled == "true"
    count = record_config_repo.set_cf_enabled_all(records, flag)
    log_service.log(
        f"Bulk action: Cloudflare DDNS {'enabled' if flag else 'disabled'} for {count} record(s).",
        level="INFO",
    )
    broadcaster.publish("records_updated", "")
    broadcaster.publish("log_appended", "{}")
    return HTMLResponse(content="", status_code=200)


@router.post("/bulk-set-unifi", response_class=HTMLResponse)
async def bulk_set_unifi(
    request: Request,
    enabled: str = Form(default="true"),
    config_service: ConfigService = Depends(get_config_service),
    record_config_repo: RecordConfigRepository = Depends(get_record_config_repo),
    log_service: LogService = Depends(get_log_service),
    broadcaster: BroadcastService = Depends(get_broadcaster),
) -> HTMLResponse:
    """
    Enables or disables UniFi DNS management for every managed record at once.

    Disabling also clears each record's ``.local`` companion flag so the
    scheduler's sync pass deletes those policies on the next cycle.  Returns
    an empty body — the dashboard's afterRequest handler reloads the page.

    Args:
        request: The incoming FastAPI request.
        enabled: "true" to enable, anything else to disable.
        config_service: Provides the managed record list.
        record_config_repo: Applies the bulk flag to RecordConfig rows.
        log_service: Writes a bulk-action log entry.
        broadcaster: Pushes a records_updated SSE event to open tabs.

    Returns:
        An empty HTMLResponse (page reload is triggered client-side).
    """
    records = await config_service.get_managed_records()
    flag = enabled == "true"
    count = record_config_repo.set_unifi_enabled_all(records, flag)
    log_service.log(
        f"Bulk action: UniFi DNS {'enabled' if flag else 'disabled'} for {count} record(s).",
        level="INFO",
    )
    broadcaster.publish("records_updated", "")
    broadcaster.publish("log_appended", "{}")
    return HTMLResponse(content="", status_code=200)


# ---------------------------------------------------------------------------
# Managed records
# ---------------------------------------------------------------------------


def _build_record_rows(
    records: list[str],
    stats_by_name: dict,
    cfgs: dict,
) -> list[dict]:
    """
    Builds the list of record dicts needed by partials/records_table.html.

    Used by add/remove/delete handlers which cannot perform live CF/UniFi API
    calls mid-action. Live IP fields are left as placeholders; the full
    dashboard page load fills them in.

    Args:
        records: List of managed FQDNs.
        stats_by_name: Dict mapping FQDN → RecordStats row (may be incomplete).
        cfgs: Dict mapping FQDN → RecordConfig (defaults filled in by repo).

    Returns:
        A list of dicts matching the template's expected record shape.
    """
    return [
        build_record_row(r, stats=stats_by_name.get(r), cfg=cfgs.get(r))
        for r in records
    ]


@router.post("/add-to-managed", response_class=HTMLResponse)
async def add_to_managed(
    request: Request,
    record_name: str = Form(...),
    init_unifi_local: str = Form(default=""),
    config_service: ConfigService = Depends(get_config_service),
    log_service: LogService = Depends(get_log_service),
    stats_service: StatsService = Depends(get_stats_service),
    record_config_repo: RecordConfigRepository = Depends(get_record_config_repo),
) -> HTMLResponse:
    """
    Adds a DNS record to the managed list and returns an updated records table.

    When init_unifi_local is "true" the record was discovered as an orphaned
    UniFi .local policy with no parent; unifi_local_enabled is pre-set so the
    scheduler starts managing the .local policy immediately.

    HTMX swaps the returned fragment into #records-container on the page.

    Args:
        request: The incoming FastAPI request.
        record_name: The FQDN to add, e.g. "home.example.com".
        init_unifi_local: When "true", pre-enable unifi_local_enabled on the new record.
        config_service: Mutates the managed-records list.
        log_service: Writes a UI log entry on success.
        stats_service: Provides current stats for the rendered table.
        record_config_repo: Provides per-record settings for each row.

    Returns:
        An HTMLResponse containing the records-table partial fragment.
    """
    added = await config_service.add_managed_record(record_name)
    if added:
        log_service.log(f"Added '{record_name}' to managed records.", level="INFO")
        # NOTE: When the record was discovered as an orphaned .local UniFi policy,
        # auto-enable unifi_local_enabled so the scheduler picks it up immediately.
        if init_unifi_local == "true":
            rc = record_config_repo.get(record_name)
            rc.unifi_local_enabled = True
            record_config_repo.save(rc)
            log_service.log(
                f"Auto-enabled UniFi .local for '{record_name}' (created from .local discovery).",
                level="INFO",
            )

    records = await config_service.get_managed_records()
    all_stats = await stats_service.get_all()
    stats_by_name = {s.record_name: s for s in all_stats}
    cfgs = record_config_repo.get_all(records)
    _, _, _, unifi_default_ip, unifi_enabled = await config_service.get_unifi_config()

    return templates.TemplateResponse(
        request,
        "partials/records_table.html",
        {
            "records": _build_record_rows(records, stats_by_name, cfgs),
            "unifi_enabled": unifi_enabled,
            "unifi_default_ip": unifi_default_ip,
        },
    )


@router.post("/add-to-managed-configured", response_class=HTMLResponse)
async def add_to_managed_configured(
    request: Request,
    record_name: str = Form(...),
    cf_enabled: str = Form(default="off"),
    ip_mode: str = Form(default="dynamic"),
    static_ip: str = Form(default=""),
    unifi_enabled: str = Form(default="off"),
    unifi_static_ip: str = Form(default=""),
    unifi_local_enabled: str = Form(default="off"),
    unifi_local_static_ip: str = Form(default=""),
    init_unifi_local: str = Form(default=""),
    config_service: ConfigService = Depends(get_config_service),
    log_service: LogService = Depends(get_log_service),
    record_config_repo: RecordConfigRepository = Depends(get_record_config_repo),
) -> HTMLResponse:
    """
    Adds a DNS record to managed and saves its initial config in one atomic step.

    Called from the unmanaged-card config modal so the user can configure
    CF/UniFi settings before the record enters the DDNS cycle.

    Args:
        request: The incoming FastAPI request.
        record_name: The FQDN to add, e.g. "home.example.com".
        cf_enabled: "on" when the Cloudflare checkbox is checked.
        ip_mode: "dynamic" or "static".
        static_ip: Fixed IP used when ip_mode is "static".
        unifi_enabled: "on" when the UniFi parent-record checkbox is checked.
        unifi_static_ip: IP for the primary UniFi DNS policy.
        unifi_local_enabled: "on" when the .local policy checkbox is checked.
        unifi_local_static_ip: Optional IP override for the .local policy.
        init_unifi_local: Legacy flag; when "true" forces unifi_local_enabled on.
        config_service: Mutates the managed-records list.
        log_service: Writes UI log entries.
        record_config_repo: Persists the initial RecordConfig.

    Returns:
        An empty HTMLResponse (SSE broadcast triggers page reload).
    """
    await config_service.add_managed_record(record_name)
    log_service.log(f"Added '{record_name}' to managed records.", level="INFO")

    cfg = record_config_repo.get(record_name)
    cfg.cf_enabled = cf_enabled == "on"
    cfg.ip_mode = ip_mode if ip_mode in ("dynamic", "static") else "dynamic"
    cfg.static_ip = static_ip.strip()
    cfg.unifi_enabled = unifi_enabled == "on"
    cfg.unifi_static_ip = unifi_static_ip.strip()
    # NOTE: init_unifi_local handles the legacy .local-only discovery path.
    cfg.unifi_local_enabled = (unifi_local_enabled == "on") or (init_unifi_local == "true")
    cfg.unifi_local_static_ip = unifi_local_static_ip.strip()
    record_config_repo.save(cfg)

    log_service.log(
        f"Initial config for '{record_name}': cf={cfg.cf_enabled} "
        f"mode={cfg.ip_mode} unifi={cfg.unifi_enabled} "
        f"unifi_local={cfg.unifi_local_enabled}",
        level="INFO",
    )

    broadcaster = getattr(request.app.state, "broadcaster", None)
    if broadcaster is not None:
        broadcaster.publish("records_updated", "")
        broadcaster.publish("log_appended", "{}")

    return HTMLResponse("")


@router.post("/remove-from-managed", response_class=HTMLResponse)
async def remove_from_managed(
    request: Request,
    record_name: str = Form(...),
    config_service: ConfigService = Depends(get_config_service),
    stats_service: StatsService = Depends(get_stats_service),
    log_service: LogService = Depends(get_log_service),
    record_config_repo: RecordConfigRepository = Depends(get_record_config_repo),
) -> HTMLResponse:
    """
    Removes a DNS record from the managed list and returns an updated records table.

    Also deletes the record's stats row so stale counts are not shown.
    HTMX swaps the returned fragment into #records-table on the page.

    Args:
        request: The incoming FastAPI request.
        record_name: The FQDN to remove.
        config_service: Mutates the managed-records list.
        stats_service: Deletes the stats row for the removed record.
        log_service: Writes a UI log entry on success.

    Returns:
        An HTMLResponse containing the records-table partial fragment.
    """
    removed = await config_service.remove_managed_record(record_name)
    if removed:
        await stats_service.delete_for_record(record_name)
        record_config_repo.delete(record_name)
        log_service.log(f"Removed '{record_name}' from managed records.", level="INFO")

    records = await config_service.get_managed_records()
    all_stats = await stats_service.get_all()
    stats_by_name = {s.record_name: s for s in all_stats}
    cfgs = record_config_repo.get_all(records)
    _, _, _, unifi_default_ip, unifi_enabled = await config_service.get_unifi_config()

    return templates.TemplateResponse(
        request,
        "partials/records_table.html",
        {
            "records": _build_record_rows(records, stats_by_name, cfgs),
            "unifi_enabled": unifi_enabled,
            "unifi_default_ip": unifi_default_ip,
        },
    )


@router.post("/delete-record", response_class=HTMLResponse)
async def delete_record(
    request: Request,
    record_id: str = Form(...),
    record_name: str = Form(...),
    config_service: ConfigService = Depends(get_config_service),
    dns_service: DnsService = Depends(get_dns_service),
    stats_service: StatsService = Depends(get_stats_service),
    log_service: LogService = Depends(get_log_service),
    record_config_repo: RecordConfigRepository = Depends(get_record_config_repo),
) -> HTMLResponse:
    """
    Deletes a DNS A-record from Cloudflare and removes it from the managed list.

    HTMX swaps the returned fragment into #records-table on the page.

    Args:
        request: The incoming FastAPI request.
        record_id: The Cloudflare record ID to delete.
        record_name: The FQDN to delete and remove from managed records.
        config_service: Removes the record from the managed list.
        dns_service: Deletes the record from the DNS provider.
        stats_service: Deletes the stats row for the deleted record.
        log_service: Writes a UI log entry on success or failure.

    Returns:
        An HTMLResponse containing the records-table partial fragment.
    """
    zones = await config_service.get_zones()
    error_message = None

    try:
        await dns_service.delete_dns_record(record_id=record_id, record_name=record_name, zones=zones)
        await config_service.remove_managed_record(record_name)
        await stats_service.delete_for_record(record_name)
        record_config_repo.delete(record_name)
        log_service.log(f"Deleted DNS record: {record_name}", level="INFO")
    except DnsProviderError as exc:
        error_message = str(exc)
        log_service.log(f"Failed to delete {record_name}: {exc}", level="ERROR")
        logger.error("delete-record failed for %s: %s", record_name, exc)

    records = await config_service.get_managed_records()
    all_stats = await stats_service.get_all()
    stats_by_name = {s.record_name: s for s in all_stats}
    cfgs = record_config_repo.get_all(records)
    _, _, _, unifi_default_ip, unifi_enabled = await config_service.get_unifi_config()

    return templates.TemplateResponse(
        request,
        "partials/records_table.html",
        {
            "records": _build_record_rows(records, stats_by_name, cfgs),
            "unifi_enabled": unifi_enabled,
            "unifi_default_ip": unifi_default_ip,
            "error_message": error_message,
        },
    )


# ---------------------------------------------------------------------------
# UniFi policy management
# ---------------------------------------------------------------------------


@router.post("/delete-unifi-record", response_class=HTMLResponse)
async def delete_unifi_record(
    request: Request,
    record_id: str = Form(...),
    record_name: str = Form(...),
    config_service: ConfigService = Depends(get_config_service),
    unifi_client: UnifiClient = Depends(get_unifi_client),
    log_service: LogService = Depends(get_log_service),
) -> HTMLResponse:
    """
    Deletes a UniFi DNS policy by its UUID.

    Used from the discovery panel to remove UniFi policies for records
    that are not in the managed list.

    Args:
        request: The incoming FastAPI request.
        record_id: The UniFi DNS policy UUID to delete.
        record_name: The FQDN of the record (used for confirmation and logging).
        config_service: Provides the UniFi site ID.
        unifi_client: Executes the delete against the UniFi controller.
        log_service: Writes a UI log entry on success or failure.

    Returns:
        An HTMLResponse with an empty body — the caller triggers a full reload.

    Raises:
        UnifiProviderError: Caught internally; returns an error fragment.
    """
    _, _, site_id, _, _ = await config_service.get_unifi_config()
    error_message: str | None = None
    try:
        await unifi_client.delete_record(zone_id=site_id, record_id=record_id)
        log_service.log(f"Deleted UniFi DNS policy: {record_name}", level="INFO")
        logger.info("Deleted UniFi policy %s (%s)", record_name, record_id)
    except UnifiProviderError as exc:
        error_message = str(exc)
        log_service.log(f"Failed to delete UniFi policy {record_name}: {exc}", level="ERROR")
        logger.error("delete-unifi-record failed for %s: %s", record_name, exc)

    if error_message:
        return HTMLResponse(
            content=f'<span style="color:#dc2626;font-size:0.85rem;">&#9888; {error_message}</span>',
            status_code=200,
        )
    # Empty response — HTMX after-request handler triggers location.reload()
    return HTMLResponse(content="", status_code=200)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@router.post("/reset-updates", response_class=HTMLResponse)
async def reset_updates(
    request: Request,
    record_name: str = Form(...),
    config_service: ConfigService = Depends(get_config_service),
    stats_service: StatsService = Depends(get_stats_service),
    record_config_repo: RecordConfigRepository = Depends(get_record_config_repo),
) -> HTMLResponse:
    """
    Resets the updates counter to zero for the given record.

    Returns the updated records-table partial so HTMX can swap it in.

    Args:
        request: The incoming FastAPI request.
        record_name: The FQDN whose updates counter to reset.
        config_service: Provides the current managed records list.
        stats_service: Resets updates and provides stats for the table.
        record_config_repo: Provides per-record config for the table.

    Returns:
        An HTMLResponse containing the records-table partial fragment.
    """
    await stats_service.reset_updates(record_name)
    logger.info("Updates reset for %s.", record_name)

    records = await config_service.get_managed_records()
    all_stats = await stats_service.get_all()
    stats_by_name = {s.record_name: s for s in all_stats}
    cfgs = record_config_repo.get_all(records)
    _, _, _, unifi_default_ip, unifi_enabled = await config_service.get_unifi_config()

    return templates.TemplateResponse(
        request,
        "partials/records_table.html",
        {
            "records": _build_record_rows(records, stats_by_name, cfgs),
            "unifi_enabled": unifi_enabled,
            "unifi_default_ip": unifi_default_ip,
        },
    )


# ---------------------------------------------------------------------------
# Per-record settings
# ---------------------------------------------------------------------------


@router.post("/update-record-config", response_class=HTMLResponse)
async def update_record_config(
    request: Request,
    record_name: str = Form(...),
    cf_enabled: str = Form(default="off"),
    ip_mode: str = Form(default="dynamic"),
    static_ip: str = Form(default=""),
    unifi_enabled: str = Form(default="off"),
    unifi_static_ip: str = Form(default=""),
    unifi_local_enabled: str = Form(default="off"),
    unifi_local_static_ip: str = Form(default=""),
    record_config_repo: RecordConfigRepository = Depends(get_record_config_repo),
    log_service: LogService = Depends(get_log_service),
) -> HTMLResponse:
    """
    Saves per-record DDNS behaviour settings and returns an updated mini-status fragment.

    Checkboxes submit "on" when checked and are absent when unchecked, so
    cf_enabled/unifi_enabled are received as strings and normalised to bools here.

    Args:
        request: The incoming FastAPI request.
        record_name: The FQDN whose config is being saved.
        cf_enabled: "on" when the Cloudflare checkbox is checked.
        ip_mode: "dynamic" or "static".
        static_ip: The fixed external IP (only used when ip_mode is "static").
        unifi_enabled: "on" when the UniFi checkbox is checked.
        unifi_static_ip: The IP used for the primary UniFi DNS policy.
        unifi_local_enabled: "on" when the optional .local policy is enabled.
        unifi_local_static_ip: Optional IP override for the .local UniFi policy.
        record_config_repo: Reads and writes RecordConfig rows.
        log_service: Writes a UI log entry on save.

    Returns:
        An HTMLResponse containing an inline save-confirmation snippet.
    """
    cfg = record_config_repo.get(record_name)
    cfg.cf_enabled = cf_enabled == "on"
    cfg.ip_mode = ip_mode if ip_mode in ("dynamic", "static") else "dynamic"
    cfg.static_ip = static_ip.strip()
    cfg.unifi_enabled = unifi_enabled == "on"
    cfg.unifi_static_ip = unifi_static_ip.strip()
    cfg.unifi_local_enabled = unifi_local_enabled == "on"
    cfg.unifi_local_static_ip = unifi_local_static_ip.strip()
    record_config_repo.save(cfg)
    log_service.log(
        f"Updated config for '{record_name}': cf={cfg.cf_enabled} "
        f"mode={cfg.ip_mode} unifi={cfg.unifi_enabled} "
        f"unifi_local={cfg.unifi_local_enabled}",
        level="INFO",
    )
    return HTMLResponse(
        '<span class="badge badge-ok" style="font-size:0.7rem;">Saved ✓</span>'
    )


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


@router.post("/clear-logs", response_class=HTMLResponse)
async def clear_logs(
    request: Request,
    log_service: LogService = Depends(get_log_service),
) -> HTMLResponse:
    """
    Deletes all log entries from the database and returns an empty log panel.

    HTMX swaps the returned fragment into #log-panel on the page.

    Args:
        request: The incoming FastAPI request.
        log_service: Deletes all log entries.

    Returns:
        An HTMLResponse containing the log-panel partial fragment.
    """
    log_service.delete_older_than(days=0)  # 0 days = delete everything
    log_service.log("Logs cleared.", level="INFO")
    recent_logs = log_service.get_recent(limit=50)

    return templates.TemplateResponse(
        request,
        "partials/log_panel.html",
        {"logs": recent_logs},
    )


# ---------------------------------------------------------------------------
# Create new DNS record
# ---------------------------------------------------------------------------


@router.post("/create-record", response_class=HTMLResponse)
async def create_record(
    request: Request,
    record_name: str = Form(...),
    record_ip: str = Form(...),
    config_service: ConfigService = Depends(get_config_service),
    dns_service: DnsService = Depends(get_dns_service),
    log_service: LogService = Depends(get_log_service),
) -> HTMLResponse:
    """
    Creates a new Cloudflare A-record and adds it to the managed list.

    On success, returns an empty 200 response. The htmx:afterRequest handler
    on the page calls safeReload() for /create-record, so no table fragment
    is needed here (returning one would flash it inside #create-record-status).
    On failure, returns a minimal error fragment with class ``alert-error``
    so the caller can display it without triggering a reload.

    Args:
        request: The incoming FastAPI request.
        record_name: The FQDN to create, e.g. "home.example.com".
        record_ip: The IPv4 address for the new record.
        config_service: Provides zones and managed records.
        dns_service: Creates the record in the DNS provider.
        log_service: Writes a UI log entry on success or failure.

    Returns:
        Empty HTMLResponse on success, or a plain error div fragment on failure.
    """
    zones = await config_service.get_zones()
    error_message = None

    try:
        await dns_service.create_dns_record(record_name, record_ip, zones)
        await config_service.add_managed_record(record_name)
        log_service.log(f"Created and managing {record_name} → {record_ip}", level="INFO")
    except DnsProviderError as exc:
        error_message = str(exc)
        log_service.log(f"Failed to create {record_name}: {exc}", level="ERROR")
        logger.error("create-record failed for %s: %s", record_name, exc)
        # NOTE: Return a minimal error fragment so the caller can display it
        # without clobbering the records table. The hx-target is the status div,
        # not #records-container, so we must not dump the full table there on failure.
        return HTMLResponse(
            content=(
                f'<div class="alert-error" style="background:#fee2e2; color:#991b1b;'
                f' border:1px solid #fca5a5; padding:0.65rem 1rem;'
                f' border-radius:0.375rem; font-size:0.875rem;">'
                f'&#9888; {error_message}</div>'
            ),
            status_code=200,
        )

    # NOTE: Return empty — the htmx:afterRequest handler calls safeReload() for
    # /create-record, so injecting any table here would flash inside #create-record-status
    # before the reload. Empty body is sufficient to trigger the reload path.
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# Per-record manual check
# ---------------------------------------------------------------------------


@router.post("/check-record", response_class=HTMLResponse)
async def check_record(
    request: Request,
    record_name: str = Form(...),
    config_service: ConfigService = Depends(get_config_service),
    dns_service: DnsService = Depends(get_dns_service),
    record_config_repo: RecordConfigRepository = Depends(get_record_config_repo),
    log_service: LogService = Depends(get_log_service),
) -> HTMLResponse:
    """
    Runs an immediate DDNS check for a single record and returns a status badge.

    Triggered by the "Check now" action on a managed record card. HTMX swaps
    the returned badge into the card's per-record status span.

    Args:
        request: The incoming FastAPI request.
        record_name: The FQDN to check.
        config_service: Provides the configured zones.
        dns_service: Runs the per-record check.
        record_config_repo: Provides the record's per-record settings.
        log_service: Writes a UI log entry on completion.

    Returns:
        An HTMLResponse containing a small status badge fragment.
    """
    zones = await config_service.get_zones()
    cfg = record_config_repo.get(record_name)
    result = await dns_service.check_record_now(record_name, cfg, zones)
    log_service.log(f"Manual check for '{record_name}': {result}.", level="INFO")

    labels = {
        "updated": ("\u2713 Updated", "#16a34a"),
        "unchanged": ("\u2713 Up to date", "#16a34a"),
        "skipped": ("Skipped", "#94a3b8"),
        "failed": ("\u26a0 Failed", "#dc2626"),
    }
    text, color = labels.get(result, ("Done", "#0284c7"))
    return HTMLResponse(
        content=f'<span class="badge" style="color:{color};">{text}</span>'
    )
