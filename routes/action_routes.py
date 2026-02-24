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
from fastapi.templating import Jinja2Templates

from dependencies import (
    get_config_service,
    get_dns_service,
    get_log_service,
    get_stats_service,
)
from exceptions import DnsProviderError
from scheduler import reschedule
from services.config_service import ConfigService
from services.dns_service import DnsService
from services.log_service import LogService
from services.stats_service import StatsService

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


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
    config_service: ConfigService = Depends(get_config_service),
    log_service: LogService = Depends(get_log_service),
) -> HTMLResponse:
    """
    Saves new Cloudflare credentials and timing configuration.

    HTMX swaps the returned fragment into #config-status on the page.

    Args:
        request: The incoming FastAPI request.
        api_token: Cloudflare API token from the config form.
        zones: JSON string of base-domain-to-zone-ID mapping.
        refresh: UI auto-refresh interval in seconds.
        interval: Background DDNS check interval in seconds.
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

    await config_service.update_credentials(
        api_token=api_token,
        zones=zones_dict,
        refresh=refresh,
        interval=interval,
    )

    # Reschedule the background job with the new interval
    reschedule(
        scheduler=request.app.state.scheduler,
        http_client=request.app.state.http_client,
        interval_seconds=interval,
    )

    log_service.log("Cloudflare configuration updated.", level="INFO")

    return templates.TemplateResponse(
        request,
        "partials/config_status.html",
        {"success": True, "message": "Configuration saved."},
    )


# ---------------------------------------------------------------------------
# Managed records
# ---------------------------------------------------------------------------


@router.post("/add-to-managed", response_class=HTMLResponse)
async def add_to_managed(
    request: Request,
    record_name: str = Form(...),
    config_service: ConfigService = Depends(get_config_service),
    log_service: LogService = Depends(get_log_service),
    stats_service: StatsService = Depends(get_stats_service),
) -> HTMLResponse:
    """
    Adds a DNS record to the managed list and returns an updated records table.

    HTMX swaps the returned fragment into #records-table on the page.

    Args:
        request: The incoming FastAPI request.
        record_name: The FQDN to add, e.g. "home.example.com".
        config_service: Mutates the managed-records list.
        log_service: Writes a UI log entry on success.
        stats_service: Provides current stats for the rendered table.

    Returns:
        An HTMLResponse containing the records-table partial fragment.
    """
    added = await config_service.add_managed_record(record_name)
    if added:
        log_service.log(f"Added '{record_name}' to managed records.", level="INFO")

    records = await config_service.get_managed_records()
    all_stats = await stats_service.get_all()
    stats_by_name = {s.record_name: s for s in all_stats}

    return templates.TemplateResponse(
        request,
        "partials/records_table.html",
        {
            "records": [
                {
                    "name": r,
                    "dns_ip": "—",
                    "is_up_to_date": None,
                    "updates": stats_by_name[r].updates if r in stats_by_name else 0,
                    "failures": stats_by_name[r].failures if r in stats_by_name else 0,
                    "last_checked": None,
                    "last_updated": None,
                }
                for r in records
            ],
        },
    )


@router.post("/remove-from-managed", response_class=HTMLResponse)
async def remove_from_managed(
    request: Request,
    record_name: str = Form(...),
    config_service: ConfigService = Depends(get_config_service),
    stats_service: StatsService = Depends(get_stats_service),
    log_service: LogService = Depends(get_log_service),
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
        log_service.log(f"Removed '{record_name}' from managed records.", level="INFO")

    records = await config_service.get_managed_records()
    all_stats = await stats_service.get_all()
    stats_by_name = {s.record_name: s for s in all_stats}

    return templates.TemplateResponse(
        request,
        "partials/records_table.html",
        {
            "records": [
                {
                    "name": r,
                    "dns_ip": "—",
                    "is_up_to_date": None,
                    "updates": stats_by_name[r].updates if r in stats_by_name else 0,
                    "failures": stats_by_name[r].failures if r in stats_by_name else 0,
                    "last_checked": None,
                    "last_updated": None,
                }
                for r in records
            ],
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
        log_service.log(f"Deleted DNS record: {record_name}", level="INFO")
    except DnsProviderError as exc:
        error_message = str(exc)
        log_service.log(f"Failed to delete {record_name}: {exc}", level="ERROR")
        logger.error("delete-record failed for %s: %s", record_name, exc)

    records = await config_service.get_managed_records()
    all_stats = await stats_service.get_all()
    stats_by_name = {s.record_name: s for s in all_stats}

    return templates.TemplateResponse(
        request,
        "partials/records_table.html",
        {
            "records": [
                {
                    "name": r,
                    "dns_ip": "—",
                    "is_up_to_date": None,
                    "updates": stats_by_name[r].updates if r in stats_by_name else 0,
                    "failures": stats_by_name[r].failures if r in stats_by_name else 0,
                    "last_checked": None,
                    "last_updated": None,
                }
                for r in records
            ],
            "error_message": error_message,
        },
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
