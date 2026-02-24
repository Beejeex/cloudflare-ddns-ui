"""
services/ip_service.py

Responsibility: Fetches the current public IP address of the host machine.
Does NOT: parse DNS records, interact with Cloudflare, or read config files.
"""

from __future__ import annotations

import logging

import httpx

from exceptions import IpFetchError

logger = logging.getLogger(__name__)

# NOTE: api.ipify.org returns the caller's public IPv4 as plain text.
_IP_PROVIDER_URL = "https://api.ipify.org"


class IpService:
    """
    Fetches the host machine's current public IPv4 address.

    Uses an injected httpx.AsyncClient so the service is fully testable
    without real network calls (use respx.mock in tests).

    Collaborators:
        - httpx.AsyncClient: injected; must be kept alive externally
    """

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        """
        Initialises the service with a shared HTTP client.

        Args:
            http_client: A long-lived httpx.AsyncClient instance created
                         during application startup.
        """
        self._client = http_client

    async def get_public_ip(self) -> str:
        """
        Returns the current public IPv4 address of the host machine.

        Returns:
            The public IP address as a plain string, e.g. "1.2.3.4".

        Raises:
            IpFetchError: If the upstream provider is unreachable or returns
                          a non-200 response.
        """
        try:
            response = await self._client.get(_IP_PROVIDER_URL)
            response.raise_for_status()
            ip = response.text.strip()
            logger.debug("Current public IP: %s", ip)
            return ip
        except httpx.HTTPStatusError as exc:
            raise IpFetchError(
                f"IP provider returned status {exc.response.status_code}."
            ) from exc
        except httpx.RequestError as exc:
            raise IpFetchError(
                f"Could not reach IP provider ({_IP_PROVIDER_URL}): {exc}"
            ) from exc
