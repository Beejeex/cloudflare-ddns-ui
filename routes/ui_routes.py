"""
routes/ui_routes.py

Responsibility: GET handlers that render full HTML pages using Jinja2 templates.
Does NOT: mutate state, return HTMX fragments, or call DNS/IP services directly.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from shared_templates import templates
from dependencies import (
    get_config_service,
    get_dns_service,
    get_ip_service,
    get_kubernetes_service,
    get_log_service,
    get_record_config_repo,
    get_stats_repo,
    get_unifi_client,
)
from exceptions import DnsProviderError, IpFetchError, KubernetesError, UnifiProviderError
from cloudflare.unifi_client import UnifiClient
from presenters import build_record_row
from repositories.record_config_repository import RecordConfigRepository
from repositories.stats_repository import StatsRepository
from services.config_service import ConfigService
from services.dns_service import DnsService
from services.ip_service import IpService
from services.kubernetes_service import KubernetesService
from services.log_service import LogService
from utils import mask_secret, to_local_policy_name

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    config_service: ConfigService = Depends(get_config_service),
    dns_service: DnsService = Depends(get_dns_service),
    stats_repo: StatsRepository = Depends(get_stats_repo),
    kubernetes_service: KubernetesService = Depends(get_kubernetes_service),
    unifi_client: UnifiClient = Depends(get_unifi_client),
    record_config_repo: RecordConfigRepository = Depends(get_record_config_repo),
    ip_service: IpService = Depends(get_ip_service),
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
    local_parent_by_name = {
        to_local_policy_name(name): name
        for name in managed_records
        if to_local_policy_name(name) != name
    }

    # Load all per-record settings up front in one query
    record_configs = record_config_repo.get_all(managed_records)

    # Detect not-yet-configured state before hitting the API
    api_error: str | None = None
    if not config.api_token or not zones:
        api_error = "No API token or zones configured. Go to Settings to set them up."

    _, _, unifi_site_id, unifi_default_ip, unifi_enabled = await config_service.get_unifi_config()

    # ---------------------------------------------------------------------
    # Fire off all independent network lookups concurrently (asyncio.gather)
    # instead of awaiting them one-by-one — page load scales with the
    # slowest provider, not the sum of all of them.
    # ---------------------------------------------------------------------

    async def _fetch_current_ip() -> str:
        """Returns the public IP, or "Unavailable" on failure (never raises)."""
        try:
            return await ip_service.get_public_ip()
        except IpFetchError as exc:
            logger.warning("Could not fetch public IP for dashboard: %s", exc)
            return "Unavailable"

    async def _fetch_unifi_policies() -> tuple[dict[str, object], str | None]:
        """Fetches all UniFi DNS policies (name → DnsRecord), or an error string."""
        if not (unifi_enabled and unifi_client.is_configured() and unifi_site_id):
            return {}, None
        try:
            policies = await unifi_client.list_records(unifi_site_id)
            return {p.name: p for p in policies}, None
        except UnifiProviderError as exc:
            logger.warning("UniFi DNS policy fetch failed: %s", exc)
            return {}, str(exc)

    async def _fetch_k8s_records() -> tuple[list, str | None]:
        """Discovers Ingress hostnames for the grid, or an error string."""
        if not kubernetes_service.is_enabled():
            return [], None
        try:
            return await kubernetes_service.list_ingress_records(), None
        except KubernetesError as exc:
            logger.warning("Kubernetes ingress discovery failed: %s", exc)
            return [], str(exc)

    async def _fetch_zone_record_map() -> tuple[dict, str | None]:
        """Bulk-fetches per-zone DNS records, or an error string."""
        if api_error:
            return {}, None
        try:
            return await dns_service.fetch_zone_record_map(managed_records, zones), None
        except DnsProviderError as exc:
            logger.warning("Could not bulk-fetch zone records for dashboard: %s", exc)
            return {}, str(exc)

    async def _fetch_zone_records() -> tuple[list, str | None]:
        """Fetches all A-records across zones for the discovery panel, or an error string."""
        if api_error:
            return [], None
        try:
            return await dns_service.list_zone_records(zones), None
        except DnsProviderError as exc:
            logger.warning("Could not fetch zone records: %s", exc)
            return [], str(exc)

    (current_ip, (unifi_policy_map, unifi_error), (k8s_records, k8s_error),
     (zone_record_map, zone_map_error), (zone_records, zone_records_error)) = await asyncio.gather(
        _fetch_current_ip(),
        _fetch_unifi_policies(),
        _fetch_k8s_records(),
        _fetch_zone_record_map(),
        _fetch_zone_records(),
    )

    # Surface a bulk CF fetch failure as the page-level API banner
    if api_error is None and zone_map_error:
        api_error = zone_map_error

    k8s_by_hostname = {r.hostname: r for r in k8s_records}

    # Build per-record display data (Cloudflare + UniFi side by side)
    record_data = []

    stats_bulk = stats_repo.get_bulk(managed_records)

    for record_name in managed_records:
        # NOTE: Match unified policy by domain name from the pre-fetched map
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
            k8s_namespace=k8s_by_hostname[record_name].namespace if record_name in k8s_by_hostname else None,
            k8s_ingress_name=k8s_by_hostname[record_name].ingress_name if record_name in k8s_by_hostname else None,
            live=True,
        ))

    # Build unified discovery list — one entry per hostname, merging CF, UniFi and K8s.
    # Keyed by hostname so sources are automatically coalesced.
    discovery_map: dict[str, dict] = {}

    def _entry(name: str) -> dict:
        return {
            "name": name,
            "cf_ip": None, "cf_record_id": None,
            "unifi_ip": None, "unifi_record_id": None,
            "unifi_local_ip": None, "unifi_local_record_id": None,
            "k8s_namespace": None, "k8s_ingress_name": None,
            "local_only": False,
        }

    # Pass 1: Add all sources into discovery_map without .local merging yet.
    # UniFi .local policies store into unifi_local_* on their own provisional entry
    # so that pass 2 can find them regardless of which source provided the parent.
    for r in zone_records:
        e = discovery_map.setdefault(r.name, _entry(r.name))
        e["cf_ip"] = r.content
        e["cf_record_id"] = r.id

    for name, policy in unifi_policy_map.items():
        e = discovery_map.setdefault(name, _entry(name))
        if name.endswith(".local"):
            # Store local data on this provisional entry; pass 2 will merge it
            # into the non-.local parent once all sources are loaded.
            e["unifi_local_ip"] = policy.content
            e["unifi_local_record_id"] = policy.id
        else:
            e["unifi_ip"] = policy.content
            e["unifi_record_id"] = policy.id

    for r in k8s_records:
        e = discovery_map.setdefault(r.hostname, _entry(r.hostname))
        e["k8s_namespace"] = r.namespace
        e["k8s_ingress_name"] = r.ingress_name

    # Pass 2: Merge standalone *.local entries into their non-.local parent card.
    # A parent is found by:
    #   1. The explicit managed-record mapping (local_parent_by_name), or
    #   2. Any existing discovery entry whose name shares the same subdomain
    #      prefix (everything before the last dot) and is not itself .local.
    # This handles the case where the parent is discovered only via K8s or CF
    # and is therefore absent from local_parent_by_name.
    # When NO parent exists at all (truly orphaned .local policy), the entry is
    # renamed to the stripped parent name so the + Manage button adds the right
    # record, and is tagged local_only=True so the template can hint the route
    # to auto-enable unifi_local_enabled.
    local_names_to_remove: list[str] = []
    for name in list(discovery_map.keys()):
        if not name.endswith(".local"):
            continue
        prefix = name[: -len(".local")]  # e.g. "headlamp.batenryck"
        parent_name: str | None = local_parent_by_name.get(name)
        if not parent_name:
            for existing in discovery_map:
                if (
                    existing != name
                    and not existing.endswith(".local")
                    and existing.startswith(prefix + ".")
                ):
                    parent_name = existing
                    break
        if parent_name and parent_name in discovery_map:
            local_entry = discovery_map[name]
            parent_entry = discovery_map[parent_name]
            # NOTE: Only copy if the parent does not already have local data set
            # by the managed-record pre-fetch path above.
            if not parent_entry["unifi_local_ip"] and local_entry["unifi_local_ip"]:
                parent_entry["unifi_local_ip"] = local_entry["unifi_local_ip"]
                parent_entry["unifi_local_record_id"] = local_entry["unifi_local_record_id"]
            local_names_to_remove.append(name)
        else:
            # No parent found anywhere — rename this entry to the reconstructed
            # FQDN (matching against configured zones) so + Manage adds the
            # correct record name (e.g. "longhorn.batenryck.net" instead of
            # the bare prefix "longhorn.batenryck"). Mark local_only=True so
            # the route auto-enables unifi_local_enabled on the new record.
            #
            # Zone match: for zone "batenryck.net" split into sld="batenryck"
            # and tld="net". If prefix ends with ".batenryck" the full FQDN
            # is reconstructed as prefix + "." + tld.
            reconstructed = prefix  # fallback: leave as-is when no zone matches
            for zone_domain in zones:
                parts = zone_domain.rsplit(".", 1)
                if len(parts) == 2:
                    sld, tld = parts
                    if prefix.endswith("." + sld) or prefix == sld:
                        reconstructed = prefix + "." + tld
                        break
            local_entry = discovery_map.pop(name)
            stripped_entry = _entry(reconstructed)
            stripped_entry["unifi_local_ip"] = local_entry["unifi_local_ip"]
            stripped_entry["unifi_local_record_id"] = local_entry["unifi_local_record_id"]
            stripped_entry["local_only"] = True
            discovery_map[reconstructed] = stripped_entry

    for name in local_names_to_remove:
        del discovery_map[name]

    discovery_records: list[dict] = sorted(discovery_map.values(), key=lambda x: x["name"])

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "current_ip": current_ip,
            "records": record_data,
            "interval": config.interval,
            "api_error": api_error,
            "first_run": bool(not config.api_token or not zones),
            "managed_names": managed_records,
            "unifi_enabled": unifi_enabled,
            "unifi_default_ip": unifi_default_ip,
            "unifi_error": unifi_error,
            "discovery_records": discovery_records,
            "zone_records_error": zone_records_error,
            "k8s_enabled": kubernetes_service.is_enabled(),
            "k8s_error": k8s_error,
        },
    )


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    log_service: LogService = Depends(get_log_service),
    config_service: ConfigService = Depends(get_config_service),
    level: str = Query(default=""),
) -> HTMLResponse:
    """
    Renders the full-page activity log viewer.

    Args:
        request: The incoming FastAPI request.
        log_service: Provides recent log entries.
        config_service: Provides the UI refresh interval for HTMX polling.
        level: Optional severity filter ("INFO", "WARNING", "ERROR"); empty = all.

    Returns:
        An HTMLResponse rendering templates/logs.html.
    """
    if level:
        recent_logs = log_service.get_by_level(level, limit=100)
    else:
        recent_logs = log_service.get_recent(limit=100)
    refresh = await config_service.get_refresh_interval()
    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "logs": recent_logs,
            "refresh": refresh,
            "level": level,
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
    config = await config_service.get_config()
    zones = await config_service.get_zones()
    refresh = await config_service.get_refresh_interval()
    unifi_host, unifi_api_key, unifi_site_id, unifi_default_ip, unifi_enabled = await config_service.get_unifi_config()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "api_token": mask_secret(config.api_token),
            "zones": json.dumps(zones),
            "interval": config.interval,
            "refresh": refresh,
            "log_retention_days": config.log_retention_days,
            "k8s_enabled": config.k8s_enabled,
            "unifi_host": unifi_host,
            "unifi_api_key": mask_secret(unifi_api_key),
            "unifi_site_id": unifi_site_id,
            "unifi_default_ip": unifi_default_ip,
            "unifi_enabled": unifi_enabled,
        },
    )
