"""
dependencies.py

Responsibility: Declares all FastAPI Depends() provider functions for
services and repositories used throughout the application.
Does NOT: contain business logic, HTTP handlers, or DB schema definitions.
"""

from __future__ import annotations

import httpx
from fastapi import Depends, Request
from sqlmodel import Session

from cloudflare.cloudflare_client import CloudflareClient
from cloudflare.dns_provider import DNSProvider
from db.database import get_session
from repositories.config_repository import ConfigRepository
from repositories.stats_repository import StatsRepository
from services.config_service import ConfigService
from services.dns_service import DnsService
from services.ip_service import IpService
from services.kubernetes_service import KubernetesService
from services.log_service import LogService
from services.stats_service import StatsService

# ---------------------------------------------------------------------------
# Infrastructure — shared app-level resources
# ---------------------------------------------------------------------------


def get_http_client(request: Request) -> httpx.AsyncClient:
    """
    Returns the shared httpx.AsyncClient stored on app.state.

    The client is created once during the FastAPI lifespan and reused for
    all requests to avoid connection-pool overhead.

    Args:
        request: The current FastAPI Request (injected automatically).

    Returns:
        The application-level httpx.AsyncClient.
    """
    return request.app.state.http_client


# ---------------------------------------------------------------------------
# Repository providers
# ---------------------------------------------------------------------------


def get_config_repo(session: Session = Depends(get_session)) -> ConfigRepository:
    """
    Provides a ConfigRepository for the current request's DB session.

    Args:
        session: The DB session injected by get_session.

    Returns:
        A ConfigRepository instance.
    """
    return ConfigRepository(session)


def get_stats_repo(session: Session = Depends(get_session)) -> StatsRepository:
    """
    Provides a StatsRepository for the current request's DB session.

    Args:
        session: The DB session injected by get_session.

    Returns:
        A StatsRepository instance.
    """
    return StatsRepository(session)


# ---------------------------------------------------------------------------
# Service providers
# ---------------------------------------------------------------------------


def get_config_service(
    config_repo: ConfigRepository = Depends(get_config_repo),
) -> ConfigService:
    """
    Provides a ConfigService backed by the current request's DB session.

    Args:
        config_repo: The repository injected by get_config_repo.

    Returns:
        A ConfigService instance.
    """
    return ConfigService(config_repo)


def get_stats_service(
    stats_repo: StatsRepository = Depends(get_stats_repo),
) -> StatsService:
    """
    Provides a StatsService backed by the current request's DB session.

    Args:
        stats_repo: The repository injected by get_stats_repo.

    Returns:
        A StatsService instance.
    """
    return StatsService(stats_repo)


def get_log_service(session: Session = Depends(get_session)) -> LogService:
    """
    Provides a LogService backed by the current request's DB session.

    Args:
        session: The DB session injected by get_session.

    Returns:
        A LogService instance.
    """
    return LogService(session)


def get_ip_service(
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> IpService:
    """
    Provides an IpService using the shared HTTP client.

    Args:
        http_client: The application-level httpx.AsyncClient.

    Returns:
        An IpService instance.
    """
    return IpService(http_client)


async def get_dns_provider(
    config_service: ConfigService = Depends(get_config_service),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> DNSProvider:
    """
    Provides a CloudflareClient initialised with the current API token.

    Loads the API token from config on every request so any token change
    takes effect without a restart.

    Args:
        config_service: Provides the current API token from the DB.
        http_client: The application-level httpx.AsyncClient.

    Returns:
        A CloudflareClient instance satisfying the DNSProvider protocol.
    """
    api_token = await config_service.get_api_token()
    return CloudflareClient(http_client=http_client, api_token=api_token)


def get_dns_service(
    dns_provider: DNSProvider = Depends(get_dns_provider),
    ip_service: IpService = Depends(get_ip_service),
    stats_service: StatsService = Depends(get_stats_service),
    log_service: LogService = Depends(get_log_service),
) -> DnsService:
    """
    Provides a fully wired DnsService for the current request.

    All collaborators are injected via Depends() so the DnsService itself
    depends only on abstractions.

    Args:
        dns_provider: The active DNSProvider implementation.
        ip_service: Provides the current public IP.
        stats_service: Records update/failure stats.
        log_service: Writes UI-visible log entries.

    Returns:
        A DnsService instance ready to use.
    """
    return DnsService(dns_provider, ip_service, stats_service, log_service)


async def get_kubernetes_service(
    config_service: ConfigService = Depends(get_config_service),
) -> KubernetesService:
    """
    Provides a KubernetesService configured with the current kubeconfig path.

    Returns a service instance regardless of whether a kubeconfig is set;
    callers must check service.is_configured() before calling list_ingress_records().

    Args:
        config_service: Provides the stored kubeconfig file path.

    Returns:
        A KubernetesService instance.
    """
    kubeconfig_path = await config_service.get_kubeconfig_path()
    return KubernetesService(kubeconfig_path=kubeconfig_path)
