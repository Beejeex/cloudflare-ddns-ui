"""
services/ip_service.py

Responsibility: Fetches the current public IP address of the host machine,
with a short-lived in-memory cache to avoid redundant upstream calls.
Does NOT: parse DNS records, interact with Cloudflare, or read config files.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from exceptions import IpFetchError

logger = logging.getLogger(__name__)

# NOTE: Providers are tried in order; a transient failure on one rolls over
# to the next so a single upstream outage does not break the DDNS cycle.
# All return the caller's public IPv4 as plain text.
_DEFAULT_IP_PROVIDER_URLS = (
    "https://api.ipify.org",
    "https://icanhazip.com",
    "https://ifconfig.me",
)

# Cache TTL in seconds.  A 30-second window collapses concurrent timer polls
# (scheduler + SSE on-connect) into a single upstream call per interval.
_CACHE_TTL = 30.0

# Retry policy for transient upstream failures (network errors and 5xx).
# A single network blip must not fail the whole DDNS cycle.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 0.2  # seconds, multiplied by (attempt + 1)


class IpService:
    """
    Fetches the host machine's current public IPv4 address.

    Results are cached on app.state.ip_cache for _CACHE_TTL seconds so that
    multiple concurrent callers (scheduler, SSE on-connect, API endpoint) share
    a single upstream call per interval instead of each issuing their own.

    Uses an injected httpx.AsyncClient so the service is fully testable
    without real network calls (use respx.mock in tests).

    Collaborators:
        - httpx.AsyncClient: injected; must be kept alive externally
        - app_state: Starlette/FastAPI app.state object; holds ip_cache dict
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        app_state: Any = None,
        ip_provider_urls: tuple[str, ...] | None = None,
    ) -> None:
        """
        Initialises the service with a shared HTTP client and optional app state.

        Args:
            http_client: A long-lived httpx.AsyncClient instance created
                         during application startup.
            app_state: The FastAPI app.state object used to store the shared
                       IP cache.  When None (e.g. in unit tests) caching is
                       disabled and every call fetches fresh.
            ip_provider_urls: Ordered list of upstream providers to try.
                              Defaults to the standard chain; tests inject a
                              single provider to disable fallback.
        """
        self._client = http_client
        self._app_state = app_state
        self._provider_urls = tuple(ip_provider_urls or _DEFAULT_IP_PROVIDER_URLS)

    async def get_public_ip(self) -> str:
        """
        Returns the current public IPv4 address of the host machine.

        Returns a cached result when app_state.ip_cache is set and the
        cached value is still within _CACHE_TTL seconds.  On a cache miss
        (or when app_state is unavailable) fetches fresh from the upstream
        provider and updates the cache.

        Returns:
            The public IP address as a plain string, e.g. "1.2.3.4".

        Raises:
            IpFetchError: If the upstream provider is unreachable or returns
                          a non-200 response.
        """
        # ---------------------------------------------------------------------------
        # Cache read — skip network call if the cached value is still fresh
        # ---------------------------------------------------------------------------
        if self._app_state is not None:
            cache = getattr(self._app_state, "ip_cache", None)
            if cache is not None:
                cached_ip: str | None = cache.get("ip")
                fetched_at: float = cache.get("fetched_at", 0.0)
                if cached_ip and (time.monotonic() - fetched_at) < _CACHE_TTL:
                    logger.debug("Public IP served from cache: %s", cached_ip)
                    return cached_ip

        # ---------------------------------------------------------------------------
        # Cache miss — fetch from upstream (with per-provider retry + fallback)
        # ---------------------------------------------------------------------------
        last_error: IpFetchError | None = None
        for provider_url in self._provider_urls:
            try:
                ip = await self._fetch_from_provider(provider_url)
                logger.debug("Current public IP fetched from upstream: %s", ip)
                break
            except IpFetchError as exc:
                # Roll over to the next provider on failure — never abort the
                # whole fetch on a single upstream outage.
                logger.warning(
                    "IP provider %s failed, trying next: %s", provider_url, exc
                )
                last_error = exc
        else:
            # No provider succeeded — surface a typed error.
            raise IpFetchError(
                f"All IP providers failed ({len(self._provider_urls)} tried): {last_error}"
            )

        # Write through to cache
        if self._app_state is not None and hasattr(self._app_state, "ip_cache"):
            self._app_state.ip_cache["ip"] = ip
            self._app_state.ip_cache["fetched_at"] = time.monotonic()

        return ip

    async def _fetch_from_provider(self, provider_url: str) -> str:
        """
        Fetches the public IP from a single provider, retrying transient errors.

        Args:
            provider_url: The provider endpoint to query.

        Returns:
            The public IP as a plain string, e.g. "1.2.3.4".

        Raises:
            IpFetchError: If the provider is unreachable or returns a
                          non-200 response after exhausting retries.
        """
        attempt = 0
        while True:
            try:
                response = await self._client.get(provider_url)
                response.raise_for_status()
                return response.text.strip()
            except httpx.HTTPStatusError as exc:
                # 5xx = transient server error → retry with backoff; 4xx = terminal.
                if exc.response.status_code >= 500 and attempt < _RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_RETRY_BACKOFF * (attempt + 1))
                    attempt += 1
                    continue
                raise IpFetchError(
                    f"IP provider returned status {exc.response.status_code}."
                ) from exc
            except httpx.RequestError as exc:
                if attempt < _RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_RETRY_BACKOFF * (attempt + 1))
                    attempt += 1
                    continue
                raise IpFetchError(
                    f"Could not reach IP provider ({provider_url}): {exc}"
                ) from exc
