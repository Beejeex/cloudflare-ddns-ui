"""
cloudflare/cloudflare_client.py

Responsibility: Implements the DNSProvider protocol using the Cloudflare REST API.
All Cloudflare HTTP calls are concentrated here — no other file may call the
Cloudflare API directly.
Does NOT: read configuration, manage stats, or contain scheduling logic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from cloudflare.dns_provider import DnsRecord, DNSProvider
from exceptions import DnsProviderError
from utils import cache_invalidate_prefix, cache_read, cache_write

logger = logging.getLogger(__name__)

_CLOUDFLARE_BASE = "https://api.cloudflare.com/client/v4"

# Records to request per page in list_records() — the Cloudflare maximum.
_LIST_PAGE_SIZE = 100

# Freshness window for the shared zone/record listing cache.  A short TTL
# collapses the scheduler cycle and the UI page load into a single listing
# fetch per interval without risking stale data for long.
_LIST_CACHE_TTL = 30.0

# Retry policy for transient API failures (network errors and 5xx).
# A single network blip must not fail the whole DDNS cycle.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 0.2  # seconds, multiplied by (attempt + 1)


class CloudflareClient:
    """
    Implements DNSProvider for the Cloudflare DNS REST API (v4).

    All outbound Cloudflare requests go through the injected httpx.AsyncClient,
    making this class fully testable without real network calls (use respx.mock).

    Collaborators:
        - httpx.AsyncClient: injected HTTP client; must be kept alive externally
        - DNSProvider: this class satisfies the protocol contract
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        api_token: str,
        cache: dict[str, tuple[float, Any]] | None = None,
        cache_ttl: float = _LIST_CACHE_TTL,
    ) -> None:
        """
        Initialises the client with an HTTP client and a Cloudflare API token.

        An optional shared cache dict (from app.state.listing_cache) lets
        concurrent callers — scheduler cycle and UI page load — share zone
        listing fetches within the TTL window instead of each hitting the API.
        Mutations invalidate the affected zone's listing automatically.

        Args:
            http_client: A long-lived httpx.AsyncClient instance.
            api_token: A Cloudflare API token with DNS edit permissions.
            cache: Optional shared listing cache; None disables caching.
            cache_ttl: Freshness window for cached listings in seconds.
        """
        self._client = http_client
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }
        self._cache = cache
        self._cache_ttl = cache_ttl

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
        self._invalidate_zone_listing(zone_id)

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
        self._invalidate_zone_listing(zone_id)

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
        self._invalidate_zone_listing(zone_id)

    async def list_records(self, zone_id: str) -> list[DnsRecord]:
        """
        Returns all A-records in the given Cloudflare zone.

        Fetches records in pages of up to 100 (the Cloudflare API maximum)
        and follows the ``result_info.total_pages`` cursor until every
        A-record in the zone has been enumerated. Without this, zones with
        more than 20 records (the API's default page size) would be silently
        truncated in the discovery panel and per-zone record maps.

        Args:
            zone_id: The Cloudflare zone ID.

        Returns:
            A list of DnsRecord instances, possibly empty.

        Raises:
            DnsProviderError: If the Cloudflare API returns an error.
        """
        url = f"{_CLOUDFLARE_BASE}/zones/{zone_id}/dns_records"
        # NOTE: per_page=100 is the maximum Cloudflare allows; pages are 1-based.
        params: dict[str, Any] = {"type": "A", "per_page": _LIST_PAGE_SIZE, "page": 1}

        cache_key = f"list_records:{zone_id}"
        cached = cache_read(self._cache, cache_key, self._cache_ttl)
        if cached is not None:
            logger.debug("Zone %s listing served from cache.", zone_id)
            return cached

        records: list[DnsRecord] = []
        seen_ids: set[str] = set()

        while True:
            logger.debug("GET %s (list, page %s)", url, params["page"])
            data = await self._request("GET", url, params=params)
            for raw in data.get("result", []):
                record = self._parse_record(raw)
                # NOTE: Dedupe by id in case page boundaries overlap when
                # records are created/deleted mid-pagination.
                if record.id not in seen_ids:
                    seen_ids.add(record.id)
                    records.append(record)

            result_info = data.get("result_info") or {}
            total_pages = int(result_info.get("total_pages") or 0)
            if total_pages and params["page"] < total_pages:
                params["page"] += 1
                continue
            # No result_info present (unusual) — assume one page holds everything.
            break

        cache_write(self._cache, cache_key, records)
        return records

    # ---------------------------------------------------------------------------
    # Cloudflare-specific helpers (not part of the DNSProvider contract)
    # ---------------------------------------------------------------------------

    async def verify_token(self) -> bool:
        """
        Verifies that the configured API token is valid and active.

        Calls GET /user/tokens/verify. Returns True when the token is active.

        Returns:
            True if the token verifies as active, False otherwise.

        Raises:
            DnsProviderError: If the API call itself fails (network/5xx).
        """
        url = f"{_CLOUDFLARE_BASE}/user/tokens/verify"
        logger.debug("GET %s (verify token)", url)
        data = await self._request("GET", url)
        result = data.get("result") or {}
        return result.get("status") == "active"

    async def list_zones(self, per_page: int = 50) -> list[dict[str, str]]:
        """
        Returns the zones accessible with the configured token.

        Used by the Settings page to auto-populate the zone list.

        Args:
            per_page: Maximum number of zones to fetch.

        Returns:
            A list of {"name", "id"} dicts, possibly empty.

        Raises:
            DnsProviderError: If the Cloudflare API returns an error.
        """
        url = f"{_CLOUDFLARE_BASE}/zones"
        params = {"per_page": per_page}
        logger.debug("GET %s (list zones)", url)

        cache_key = f"list_zones:{per_page}"
        cached = cache_read(self._cache, cache_key, self._cache_ttl)
        if cached is not None:
            return cached

        data = await self._request("GET", url, params=params)
        zones = [
            {"name": z.get("name", ""), "id": z.get("id", "")}
            for z in data.get("result", [])
        ]
        cache_write(self._cache, cache_key, zones)
        return zones

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    def _invalidate_zone_listing(self, zone_id: str) -> None:
        """
        Drops the cached listing for a zone after a mutation.

        Ensures the next read re-fetches authoritative state instead of
        serving a stale list that no longer reflects the write.

        Args:
            zone_id: The Cloudflare zone whose listing to invalidate.

        Returns:
            None
        """
        cache_invalidate_prefix(self._cache, f"list_records:{zone_id}")

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
        attempt = 0
        while True:
            try:
                response = await self._client.request(
                    method, url, headers=self._headers, params=params, json=json
                )
                response.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                # 5xx = transient server error → retry with backoff; 4xx = terminal.
                if exc.response.status_code >= 500 and attempt < _RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_RETRY_BACKOFF * (attempt + 1))
                    attempt += 1
                    continue
                raise DnsProviderError(
                    f"Cloudflare API error {exc.response.status_code} for {method} {url}: "
                    f"{exc.response.text}"
                ) from exc
            except httpx.RequestError as exc:
                if attempt < _RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_RETRY_BACKOFF * (attempt + 1))
                    attempt += 1
                    continue
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
