"""
routes/ui_routes.py

Responsibility: GET handlers that render full HTML pages using Jinja2 templates.
Does NOT: mutate state, return HTMX fragments, or call DNS/IP services directly.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from dependencies import (
    get_config_service,
    get_dns_service,
    get_kubernetes_service,
    get_log_service,
    get_stats_service,
)
from exceptions import DnsProviderError, IpFetchError, KubernetesError
from services.config_service import ConfigService
from services.dns_service import DnsService
from services.kubernetes_service import KubernetesService
from services.log_service import LogService
from services.stats_service import StatsService

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    config_service: ConfigService = Depends(get_config_service),
    dns_service: DnsService = Depends(get_dns_service),
    stats_service: StatsService = Depends(get_stats_service),
    kubernetes_service: KubernetesService = Depends(get_kubernetes_service),
) -> HTMLResponse:
    """
    Renders the main DDNS dashboard page.

    Shows per-record DNS status, stats, a live countdown to the next check,
    and (when configured) hostnames discovered from Kubernetes Ingress resources.

    Args:
        request: The incoming FastAPI request.
        config_service: Provides application configuration.
        dns_service: Fetches live record state from the DNS provider.
        stats_service: Provides per-record update/failure stats.
        kubernetes_service: Discovers hostnames from cluster Ingress resources.

    Returns:
        An HTMLResponse rendering templates/dashboard.html.
    """
    config = await config_service.get_config()
    zones = await config_service.get_zones()
    managed_records = await config_service.get_managed_records()

    # Fetch current public IP — display "Unavailable" on failure rather than 500
    current_ip = "Unavailable"
    try:
        from services.ip_service import IpService
        ip_service = IpService(request.app.state.http_client)
        current_ip = await ip_service.get_public_ip()
    except IpFetchError as exc:
        logger.warning("Could not fetch public IP for dashboard: %s", exc)

    # Detect not-yet-configured state before hitting the API
    api_error: str | None = None
    if not config.api_token or not zones:
        api_error = "No API token or zones configured. Go to Settings to set them up."

    # Build per-record display data
    record_data = []
    for record_name in managed_records:
        dns_record = None
        try:
            dns_record = await dns_service.check_single_record(record_name, zones)
        except DnsProviderError as exc:
            logger.warning("Could not fetch DNS record %s: %s", record_name, exc)
            if not api_error:
                api_error = str(exc)

        stats = await stats_service.get_for_record(record_name)
        dns_ip = dns_record.content if dns_record else "Not Found"
        is_up_to_date = dns_record is not None and dns_ip == current_ip

        record_data.append({
            "name": record_name,
            "dns_ip": dns_ip,
            "is_up_to_date": is_up_to_date,
            "updates": stats.updates if stats else 0,
            "failures": stats.failures if stats else 0,
            "last_checked": stats.last_checked.isoformat() if stats and stats.last_checked else None,
            "last_updated": stats.last_updated.isoformat() if stats and stats.last_updated else None,
        })

    # Fetch all A-records for the "add record" table
    all_records = []
    try:
        all_records = await dns_service.list_zone_records(zones)
    except DnsProviderError as exc:
        logger.warning("Could not list zone records for dashboard: %s", exc)
        if not api_error:
            api_error = str(exc)

    # Discover hostnames from Kubernetes Ingress resources (optional feature)
    k8s_records: list = []
    k8s_error: str | None = None
    if kubernetes_service.is_enabled():
        try:
            k8s_records = await kubernetes_service.list_ingress_records()
        except KubernetesError as exc:
            logger.warning("Kubernetes ingress discovery failed: %s", exc)
            k8s_error = str(exc)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "current_ip": current_ip,
            "records": record_data,
            "all_records": all_records,
            "interval": config.interval,
            "api_error": api_error,
            "k8s_records": k8s_records,
            "k8s_error": k8s_error,
            "managed_names": managed_records,
        },
    )


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    log_service: LogService = Depends(get_log_service),
    config_service: ConfigService = Depends(get_config_service),
) -> HTMLResponse:
    """
    Renders the full-page activity log viewer.

    Args:
        request: The incoming FastAPI request.
        log_service: Provides recent log entries.
        config_service: Provides the UI refresh interval for HTMX polling.

    Returns:
        An HTMLResponse rendering templates/logs.html.
    """
    recent_logs = log_service.get_recent(limit=200)
    refresh = await config_service.get_refresh_interval()
    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "logs": recent_logs,
            "refresh": refresh,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    config_service: ConfigService = Depends(get_config_service),
) -> HTMLResponse:
    """
    Renders the settings / configuration page.

    Args:
        request: The incoming FastAPI request.
        config_service: Provides current application configuration.

    Returns:
        An HTMLResponse rendering templates/settings.html.
    """
    import json
    config = await config_service.get_config()
    zones = await config_service.get_zones()
    refresh = await config_service.get_refresh_interval()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "api_token": config.api_token,
            "zones": json.dumps(zones),
            "interval": config.interval,
            "refresh": refresh,
            "k8s_enabled": config.k8s_enabled,
        },
    )
