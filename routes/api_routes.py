"""
routes/api_routes.py

Responsibility: JSON and HTMX partial API endpoints consumed by the frontend
for live polling (log tail, IP status). These are lightweight read-only endpoints.
Does NOT: mutate state, render full pages, or perform DNS updates.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from cloudflare.unifi_client import UnifiClient
from dependencies import (
    get_config_service,
    get_log_service,
    get_stats_service,
    get_unifi_http_client,
)
from exceptions import IpFetchError, UnifiProviderError
from services.config_service import ConfigService
from services.log_service import LogService
from services.stats_service import StatsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")
templates = Jinja2Templates(directory="templates")


@router.get("/logs/recent", response_class=HTMLResponse)
async def get_recent_logs(
    request: Request,
    log_service: LogService = Depends(get_log_service),
) -> HTMLResponse:
    """
    Returns the recent log entries as an HTML fragment for HTMX polling.

    The dashboard polls this endpoint every N seconds (configured via the
    hx-trigger attribute on the log panel) and swaps in the result.

    Args:
        request: The incoming FastAPI request.
        log_service: Provides recent log entries from the DB.

    Returns:
        An HTMLResponse containing the log-panel partial fragment.
    """
    recent_logs = log_service.get_recent(limit=50)
    return templates.TemplateResponse(
        request,
        "partials/log_panel.html",
        {"logs": recent_logs},
    )


@router.get("/status", response_class=HTMLResponse)
async def get_status(
    request: Request,
    config_service: ConfigService = Depends(get_config_service),
    stats_service: StatsService = Depends(get_stats_service),
) -> HTMLResponse:
    """
    Returns current IP and record status as an HTML fragment for HTMX polling.

    Args:
        request: The incoming FastAPI request.
        config_service: Provides the managed records list and refresh interval.
        stats_service: Provides up-to-date stats per record.

    Returns:
        An HTMLResponse containing the status-bar partial fragment.
    """
    # Fetch current public IP — show "Unavailable" rather than raising
    current_ip = "Unavailable"
    try:
        from services.ip_service import IpService
        ip_service = IpService(request.app.state.http_client)
        current_ip = await ip_service.get_public_ip()
    except IpFetchError as exc:
        logger.warning("Could not fetch public IP for status endpoint: %s", exc)

    all_stats = await stats_service.get_all()

    return templates.TemplateResponse(
        request,
        "partials/status_bar.html",
        {
            "current_ip": current_ip,
            "stats": all_stats,
        },
    )


@router.get("/current-ip", response_class=PlainTextResponse)
async def current_ip(request: Request) -> str:
    """
    Returns the host's current public IP as plain text for the navbar HTMX poll.

    Args:
        request: The incoming FastAPI request.

    Returns:
        The public IP address string, or "Unavailable" on failure.
    """
    try:
        from services.ip_service import IpService
        ip_service = IpService(request.app.state.http_client)
        return await ip_service.get_public_ip()
    except IpFetchError as exc:
        logger.warning("Could not fetch public IP for navbar: %s", exc)
        return "Unavailable"


@router.get("/unifi/sites", response_class=HTMLResponse)
async def get_unifi_sites(
    request: Request,
    unifi_host: str = Query(default="", alias="unifi_host"),
    unifi_api_key: str = Query(default="", alias="unifi_api_key"),
    http_client: httpx.AsyncClient = Depends(get_unifi_http_client),
) -> HTMLResponse:
    """
    Queries the UniFi controller for all available sites and returns an HTML
    partial so the settings page can auto-fill or show a picker for the Site ID.

    Accepts the host and api_key as query parameters so the user does not need
    to save settings first.

    Args:
        unifi_host: UniFi controller host (IP or hostname).
        unifi_api_key: UniFi API key.
        http_client: Shared async client with verify=False.

    Returns:
        HTML partial rendered from partials/unifi_sites.html.
    """
    context: dict = {"request": request, "sites": [], "error": None}
    if not unifi_host or not unifi_api_key:
        context["error"] = "Enter a host and API key first."
    else:
        client = UnifiClient(http_client=http_client, api_key=unifi_api_key, host=unifi_host)
        try:
            context["sites"] = await client.list_sites()
        except UnifiProviderError as exc:
            logger.warning("UniFi site discovery failed: %s", exc)
            context["error"] = str(exc)
    return templates.TemplateResponse("partials/unifi_sites.html", context)


@router.get("/health/json")
async def health_json() -> dict:
    """
    Returns application health as a JSON response.

    Returns:
        A dict with a "status" key set to "ok".
    """
    return {"status": "ok"}


@router.get("/next-check-in")
async def next_check_in(request: Request) -> dict:
    """
    Returns the seconds remaining until the next scheduled DDNS check.

    Reads the live next_run_time from APScheduler so the dashboard countdown
    stays accurate across page refreshes.

    Args:
        request: The incoming FastAPI request.

    Returns:
        A dict with "seconds" (int) and "interval" (int) keys.
    """
    from datetime import datetime, timezone
    from repositories.config_repository import ConfigRepository
    from db.database import engine
    from sqlmodel import Session

    interval = 300
    try:
        with Session(engine) as session:
            interval = ConfigRepository(session).load().interval
    except Exception:
        pass

    seconds_remaining = interval
    try:
        scheduler = request.app.state.scheduler
        job = scheduler.get_job("ddns_check")
        if job and job.next_run_time:
            delta = job.next_run_time - datetime.now(timezone.utc)
            seconds_remaining = max(0, int(delta.total_seconds()))
    except Exception as exc:
        logger.debug("Could not read scheduler next_run_time: %s", exc)

    return {"seconds": seconds_remaining, "interval": interval}
