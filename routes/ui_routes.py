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
    get_unifi_client,
)
from exceptions import DnsProviderError, IpFetchError, KubernetesError, UnifiProviderError
from cloudflare.unifi_client import UnifiClient
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
    unifi_client: UnifiClient = Depends(get_unifi_client),
) -> HTMLResponse:
    """
    Renders the main DDNS dashboard page.

    Shows per-record DNS status across Cloudflare and UniFi, stats,
    a live countdown to the next check, and (when enabled) hostnames
    discovered from Kubernetes Ingress resources.

    Args:
        request: The incoming FastAPI request.
        config_service: Provides application configuration.
        dns_service: Fetches live record state from the DNS provider.
        stats_service: Provides per-record update/failure stats.
        kubernetes_service: Discovers hostnames from cluster Ingress resources.
        unifi_client: Fetches internal DNS policies from UniFi.

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

    # Build per-record display data (Cloudflare + UniFi side by side)
    record_data = []

    # Fetch all UniFi DNS policies in one call upfront (avoid N per-record requests)
    unifi_error: str | None = None
    unifi_policy_map: dict[str, object] = {}
    _, unifi_site_id, unifi_enabled = await config_service.get_unifi_config()
    if unifi_enabled and unifi_client.is_configured() and unifi_site_id:
        try:
            policies = await unifi_client.list_records(unifi_site_id)
            unifi_policy_map = {p.name: p for p in policies}
        except UnifiProviderError as exc:
            logger.warning("UniFi DNS policy fetch failed: %s", exc)
            unifi_error = str(exc)

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

        # NOTE: Match unified policy by domain name from the pre-fetched map
        unifi_policy = unifi_policy_map.get(record_name)

        record_data.append({
            "name": record_name,
            "dns_ip": dns_ip,
            "is_up_to_date": is_up_to_date,
            "updates": stats.updates if stats else 0,
            "failures": stats.failures if stats else 0,
            "last_checked": stats.last_checked.isoformat() if stats and stats.last_checked else None,
            "last_updated": stats.last_updated.isoformat() if stats and stats.last_updated else None,
            "unifi_ip": unifi_policy.content if unifi_policy else None,
            "unifi_record_id": unifi_policy.id if unifi_policy else None,
        })

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
            "interval": config.interval,
            "api_error": api_error,
            "k8s_records": k8s_records,
            "k8s_error": k8s_error,
            "managed_names": managed_records,
            "unifi_enabled": unifi_enabled,
            "unifi_error": unifi_error,
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
    unifi_api_key, unifi_site_id, unifi_enabled = await config_service.get_unifi_config()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "api_token": config.api_token,
            "zones": json.dumps(zones),
            "interval": config.interval,
            "refresh": refresh,
            "k8s_enabled": config.k8s_enabled,
            "unifi_api_key": unifi_api_key,
            "unifi_site_id": unifi_site_id,
            "unifi_enabled": unifi_enabled,
        },
    )
