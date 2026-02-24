"""
cloudflare/cloudflare_client.py

Responsibility: Implements the DNSProvider protocol using the Cloudflare REST API.
All Cloudflare HTTP calls are concentrated here — no other file may call the
Cloudflare API directly.
Does NOT: read configuration, manage stats, or contain scheduling logic.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from cloudflare.dns_provider import DnsRecord, DNSProvider
from exceptions import DnsProviderError

logger = logging.getLogger(__name__)

_CLOUDFLARE_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareClient:
    """
    Implements DNSProvider for the Cloudflare DNS REST API (v4).

    All outbound Cloudflare requests go through the injected httpx.AsyncClient,
    making this class fully testable without real network calls (use respx.mock).

    Collaborators:
        - httpx.AsyncClient: injected HTTP client; must be kept alive externally
        - DNSProvider: this class satisfies the protocol contract
    """

    def __init__(self, http_client: httpx.AsyncClient, api_token: str) -> None:
        """
        Initialises the client with an HTTP client and a Cloudflare API token.

        Args:
            http_client: A long-lived httpx.AsyncClient instance.
            api_token: A Cloudflare API token with DNS edit permissions.
        """
        self._client = http_client
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    # ---------------------------------------------------------------------------
    # DNSProvider implementation
    # ---------------------------------------------------------------------------

    async def get_record(self, zone_id: str, record_name: str) -> DnsRecord | None:
        """
        Fetches a single A-record by name within the given Cloudflare zone.

        Args:
            zone_id: The Cloudflare zone ID.
            record_name: The fully-qualified DNS name to look up.

        Returns:
            A DnsRecord if the record exists, or None if not found.

        Raises:
            DnsProviderError: If the Cloudflare API returns an error.
        """
        url = f"{_CLOUDFLARE_BASE}/zones/{zone_id}/dns_records"
        params = {"type": "A", "name": record_name}

        logger.debug("GET %s params=%s", url, params)
        data = await self._request("GET", url, params=params)

        result = data.get("result", [])
        if not result:
            return None

        return self._parse_record(result[0])

    async def update_record(self, zone_id: str, record: DnsRecord, new_ip: str) -> DnsRecord:
        """
        Updates an existing A-record with a new IP address.

        Args:
            zone_id: The Cloudflare zone ID.
            record: The existing DnsRecord to update.
            new_ip: The new IPv4 address to write.

        Returns:
            The updated DnsRecord.

        Raises:
            DnsProviderError: If the Cloudflare API returns an error.
        """
        url = f"{_CLOUDFLARE_BASE}/zones/{zone_id}/dns_records/{record.id}"
        payload: dict[str, Any] = {
            "type": "A",
            "name": record.name,
            "content": new_ip,
            "ttl": record.ttl,
            "proxied": record.proxied,
        }

        logger.debug("PUT %s payload=%s", url, payload)
        data = await self._request("PUT", url, json=payload)

        return self._parse_record(data["result"])

    async def create_record(self, zone_id: str, record_name: str, ip: str) -> DnsRecord:
        """
        Creates a new A-record in the given Cloudflare zone.

        Args:
            zone_id: The Cloudflare zone ID.
            record_name: The fully-qualified DNS name for the new record.
            ip: The IPv4 address for the new record.

        Returns:
            The newly created DnsRecord.

        Raises:
            DnsProviderError: If the Cloudflare API returns an error.
        """
        url = f"{_CLOUDFLARE_BASE}/zones/{zone_id}/dns_records"
        payload: dict[str, Any] = {
            "type": "A",
            "name": record_name,
            "content": ip,
            "ttl": 1,      # 1 = automatic TTL on Cloudflare
            "proxied": False,
        }

        logger.debug("POST %s payload=%s", url, payload)
        data = await self._request("POST", url, json=payload)

        return self._parse_record(data["result"])

    async def delete_record(self, zone_id: str, record_id: str) -> None:
        """
        Deletes a DNS record from the given Cloudflare zone.

        Args:
            zone_id: The Cloudflare zone ID.
            record_id: The Cloudflare-assigned unique record identifier.

        Returns:
            None

        Raises:
            DnsProviderError: If the Cloudflare API returns an error.
        """
        url = f"{_CLOUDFLARE_BASE}/zones/{zone_id}/dns_records/{record_id}"

        logger.debug("DELETE %s", url)
        await self._request("DELETE", url)

    async def list_records(self, zone_id: str) -> list[DnsRecord]:
        """
        Returns all A-records in the given Cloudflare zone.

        Args:
            zone_id: The Cloudflare zone ID.

        Returns:
            A list of DnsRecord instances, possibly empty.

        Raises:
            DnsProviderError: If the Cloudflare API returns an error.
        """
        url = f"{_CLOUDFLARE_BASE}/zones/{zone_id}/dns_records"
        params = {"type": "A"}

        logger.debug("GET %s (list)", url)
        data = await self._request("GET", url, params=params)

        return [self._parse_record(r) for r in data.get("result", [])]

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Sends an authenticated HTTP request to the Cloudflare API.

        Args:
            method: HTTP verb ("GET", "PUT", "POST", "DELETE").
            url: Full URL of the Cloudflare API endpoint.
            params: Optional query-string parameters.
            json: Optional JSON request body.

        Returns:
            The parsed JSON response body as a dict.

        Raises:
            DnsProviderError: If the HTTP call fails or the API returns
                              success=false in the response body.
        """
        try:
            response = await self._client.request(
                method, url, headers=self._headers, params=params, json=json
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise DnsProviderError(
                f"Cloudflare API error {exc.response.status_code} for {method} {url}: "
                f"{exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise DnsProviderError(
                f"Network error calling Cloudflare API ({method} {url}): {exc}"
            ) from exc

        body: dict[str, Any] = response.json()

        # NOTE: Cloudflare wraps all responses in {"success": bool, "result": ...}
        if not body.get("success", False):
            errors = body.get("errors", [])
            raise DnsProviderError(
                f"Cloudflare API returned success=false for {method} {url}. "
                f"Errors: {errors}"
            )

        return body

    @staticmethod
    def _parse_record(raw: dict[str, Any]) -> DnsRecord:
        """
        Converts a raw Cloudflare API record dict into a typed DnsRecord.

        Args:
            raw: A single record object from the Cloudflare API response.

        Returns:
            A DnsRecord populated from the raw dict.
        """
        return DnsRecord(
            id=raw["id"],
            name=raw["name"],
            content=raw["content"],
            type=raw.get("type", "A"),
            ttl=raw.get("ttl", 1),
            proxied=raw.get("proxied", False),
            zone_id=raw.get("zone_id", ""),
        )
