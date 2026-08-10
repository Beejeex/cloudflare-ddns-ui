"""
cloudflare/unifi_client.py

Responsibility: Implements the DNSProvider protocol using the local UniFi Network
Application REST API (https://{host}/proxy/network/integration/v1). Manages DNS
Policies on a single UniFi site. SSL verification is disabled because UniFi
controllers use self-signed certificates.
Does NOT: read configuration from the database, manage stats, or schedule jobs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from cloudflare.dns_provider import DnsRecord, DNSProvider
from exceptions import UnifiProviderError
from utils import cache_invalidate_prefix, cache_read, cache_write

logger = logging.getLogger(__name__)

# Path suffix appended to the controller host to reach the integration API.
# Exported so tests can construct expected URLs without duplicating the string.
_UNIFI_PATH = "/proxy/network/integration/v1"

# Freshness window for the shared site/policy listing cache.  A short TTL
# collapses the scheduler sync pass and the UI page load into a single
# listing fetch per interval without risking stale data for long.
_LIST_CACHE_TTL = 30.0

# TTL of 0 = auto (inherits from the UniFi site's global DNS TTL setting)
_DEFAULT_TTL = 0

# Maximum records to fetch in a single list call (UniFi API max is 200)
_LIST_LIMIT = 200

# Defensive cap on the number of pagination iterations per list_records() call.
_MAX_PAGES = 50

# Retry policy for transient controller failures (network errors and 5xx).
# The UniFi controller is a local appliance that can be briefly unavailable
# during a reboot — a single blip must not fail the sync pass.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 0.2  # seconds, multiplied by (attempt + 1)


class UnifiClient:
    """
    Implements DNSProvider for the UniFi Network Site Manager DNS Policies API.

    Maps UniFi DNS Policy concepts to the shared DnsRecord value object:
        - zone_id   → UniFi siteId (UUID)
        - record.id → UniFi dnsPolicyId (UUID)
        - name      → domain
        - content   → ipv4Address
        - ttl       → ttlSeconds
        - proxied   → always False (UniFi has no CDN proxy concept)

    All outbound calls go through the injected httpx.AsyncClient.

    Collaborators:
        - httpx.AsyncClient: injected HTTP client; kept alive externally
        - DNSProvider: this class satisfies the protocol contract
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        api_key: str,
        host: str,
        cache: dict[str, tuple[float, Any]] | None = None,
        cache_ttl: float = _LIST_CACHE_TTL,
    ) -> None:
        """
        Initialises the client with an HTTP client, API key, and controller host.

        An optional shared cache dict (from app.state.listing_cache) lets
        concurrent callers share site/policy listing fetches within the TTL
        window instead of each hitting the controller.  Mutations invalidate
        the affected site's listing automatically.

        Args:
            http_client: A long-lived httpx.AsyncClient instance (must have verify=False).
            api_key: A UniFi API key with DNS write access.
            host: Hostname or IP of the local UniFi Network Application,
                  e.g. "192.168.1.1" or "unifi.local".
            cache: Optional shared listing cache; None disables caching.
            cache_ttl: Freshness window for cached listings in seconds.
        """
        self._client = http_client
        # Build the base URL once; strip trailing slash to avoid double-slash URLs.
        self._base = f"https://{host.rstrip('/')}{_UNIFI_PATH}"
        self._headers = {
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._cache = cache
        self._cache_ttl = cache_ttl

    # ---------------------------------------------------------------------------
    # Public helper
    # ---------------------------------------------------------------------------

    def is_configured(self) -> bool:
        """
        Returns True if an API key has been set.

        Returns:
            True if the api_key is non-empty, False otherwise.
        """
        return bool(self._headers.get("X-API-KEY", "").strip())

    # ---------------------------------------------------------------------------
    # DNSProvider implementation
    # ---------------------------------------------------------------------------

    async def get_record(self, zone_id: str, record_name: str) -> DnsRecord | None:
        """
        Finds a DNS policy whose domain matches record_name on the given site.

        UniFi has no direct lookup-by-name endpoint, so this fetches the full
        list and filters. The list is small enough that this is acceptable.

        Args:
            zone_id: The UniFi site UUID.
            record_name: The DNS domain to look up (e.g. "home.example.com").

        Returns:
            A DnsRecord if a matching policy exists, or None if not found.

        Raises:
            UnifiProviderError: If the API call fails.
        """
        records = await self.list_records(zone_id)
        for record in records:
            if record.name == record_name:
                return record
        return None

    async def update_record(self, zone_id: str, record: DnsRecord, new_ip: str) -> DnsRecord:
        """
        Updates an existing DNS policy with a new IP address.

        Args:
            zone_id: The UniFi site UUID.
            record: The existing DnsRecord to update (record.id must be the policy UUID).
            new_ip: The new IPv4 address to write.

        Returns:
            The updated DnsRecord.

        Raises:
            UnifiProviderError: If the API call fails.
        """
        url = f"{self._base}/sites/{zone_id}/dns/policies/{record.id}"
        payload: dict[str, Any] = {
            "type": "A_RECORD",
            "enabled": True,
            "domain": record.name,
            "ipv4Address": new_ip,
            "ttlSeconds": record.ttl if record.ttl else _DEFAULT_TTL,
        }
        logger.debug("PUT %s payload=%s", url, payload)
        data = await self._request("PUT", url, json=payload)
        self._invalidate_site_listing(zone_id)
        return self._parse_policy(data)

    async def create_record(self, zone_id: str, record_name: str, ip: str) -> DnsRecord:
        """
        Creates a new DNS policy on the given site.

        Args:
            zone_id: The UniFi site UUID.
            record_name: The DNS domain for the new policy (e.g. "home.example.com").
            ip: The IPv4 address for the new policy.

        Returns:
            The newly created DnsRecord.

        Raises:
            UnifiProviderError: If the API call fails.
        """
        url = f"{self._base}/sites/{zone_id}/dns/policies"
        payload: dict[str, Any] = {
            "type": "A_RECORD",
            "enabled": True,
            "domain": record_name,
            "ipv4Address": ip,
            "ttlSeconds": _DEFAULT_TTL,
        }
        logger.debug("POST %s payload=%s", url, payload)
        data = await self._request("POST", url, json=payload)
        self._invalidate_site_listing(zone_id)
        return self._parse_policy(data)

    async def delete_record(self, zone_id: str, record_id: str) -> None:
        """
        Deletes a DNS policy from the given site.

        Args:
            zone_id: The UniFi site UUID.
            record_id: The UniFi DNS policy UUID.

        Returns:
            None

        Raises:
            UnifiProviderError: If the API call fails.
        """
        url = f"{self._base}/sites/{zone_id}/dns/policies/{record_id}"
        logger.debug("DELETE %s", url)
        await self._request("DELETE", url)
        self._invalidate_site_listing(zone_id)

    async def list_sites(self) -> list[dict[str, str]]:
        """
        Returns all sites registered on this UniFi controller.

        Calls GET /sites and normalises the response to a list of dicts with
        "id" and "name" keys regardless of how the controller names those fields.

        Returns:
            A list of dicts: [{"id": "<uuid>", "name": "<display-name>"}].

        Raises:
            UnifiProviderError: If the API call fails.
        """
        url = f"{self._base}/sites"
        logger.debug("GET %s (list sites)", url)

        cache_key = "list_sites"
        cached = cache_read(self._cache, cache_key, self._cache_ttl)
        if cached is not None:
            return cached

        data = await self._request("GET", url)
        sites: list[dict[str, str]] = []
        for s in data.get("data", []):
            # NOTE: Different controller versions use "siteId" or "id" for the UUID.
            site_id = s.get("siteId") or s.get("id", "")
            # UniFi uses "internalReference" for the default site; fall back to "name".
            name = s.get("name") or s.get("internalReference") or site_id[:8]
            sites.append({"id": site_id, "name": name})
        cache_write(self._cache, cache_key, sites)
        return sites

    async def list_records(self, zone_id: str) -> list[DnsRecord]:
        """
        Returns all A-record DNS policies on the given site.

        Fetches policies in pages of 200 (the UniFi API maximum per request)
        and follows the ``totalCount`` field until every policy has been read,
        so sites with more than 200 policies are fully enumerated.

        Args:
            zone_id: The UniFi site UUID.

        Returns:
            A list of DnsRecord instances, possibly empty.

        Raises:
            UnifiProviderError: If the API call fails.
        """
        url = f"{self._base}/sites/{zone_id}/dns/policies"
        cache_key = f"list_records:{zone_id}"
        cached = cache_read(self._cache, cache_key, self._cache_ttl)
        if cached is not None:
            logger.debug("Site %s listing served from cache.", zone_id)
            return cached

        records: list[DnsRecord] = []
        seen_ids: set[str] = set()
        offset = 0

        while True:
            params = {"limit": _LIST_LIMIT, "offset": offset}
            logger.debug("GET %s params=%s", url, params)
            data = await self._request("GET", url, params=params)

            page_count = len(data.get("data", []))
            for policy in data.get("data", []):
                # NOTE: Only A_RECORD type is relevant — skip CNAME, MX, etc.
                if policy.get("type") != "A_RECORD":
                    continue
                record = self._parse_policy(policy)
                # NOTE: Dedupe by id in case page boundaries overlap.
                if record.id not in seen_ids:
                    seen_ids.add(record.id)
                    records.append(record)

            total_count = data.get("totalCount") or 0
            offset += _LIST_LIMIT
            # Stop when a short page signals the end of data, or once we have
            # walked past the reported total. Cap iterations defensively so a
            # misbehaving controller can never cause an unbounded loop.
            if page_count < _LIST_LIMIT or offset >= total_count or offset > _MAX_PAGES * _LIST_LIMIT:
                break

        cache_write(self._cache, cache_key, records)
        return records

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    def _invalidate_site_listing(self, zone_id: str) -> None:
        """
        Drops the cached policy listing for a site after a mutation.

        Ensures the next read re-fetches authoritative state instead of
        serving a stale list that no longer reflects the write.

        Args:
            zone_id: The UniFi site UUID whose listing to invalidate.

        Returns:
            None
        """
        cache_invalidate_prefix(self._cache, f"list_records:{zone_id}")

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Executes an HTTP request and returns the parsed JSON body.

        Args:
            method: HTTP method string ("GET", "POST", "PUT", "DELETE").
            url: Full request URL.
            params: Optional query parameters.
            json: Optional JSON request body.

        Returns:
            Parsed JSON response as a dict (empty dict for 204/no-body responses).

        Raises:
            UnifiProviderError: On HTTP error, connection failure, or non-2xx response.
        """
        attempt = 0
        while True:
            try:
                response = await self._client.request(
                    method,
                    url,
                    headers=self._headers,
                    params=params,
                    json=json,
                )
                response.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                # 5xx = transient controller error → retry with backoff; 4xx = terminal.
                if exc.response.status_code >= 500 and attempt < _RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_RETRY_BACKOFF * (attempt + 1))
                    attempt += 1
                    continue
                raise UnifiProviderError(
                    f"UniFi API {exc.response.status_code} for {method} {url}: "
                    f"{exc.response.text[:200]}"
                ) from exc
            except httpx.RequestError as exc:
                if attempt < _RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_RETRY_BACKOFF * (attempt + 1))
                    attempt += 1
                    continue
                raise UnifiProviderError(
                    f"UniFi API connection error for {method} {url}: {exc}"
                ) from exc

        # DELETE returns 204 with no body
        if response.status_code == 204 or not response.content:
            return {}

        return response.json()

    @staticmethod
    def _parse_policy(policy: dict[str, Any]) -> DnsRecord:
        """
        Converts a UniFi DNS policy dict to a DnsRecord value object.

        Args:
            policy: Raw dict from the UniFi DNS Policies API response.

        Returns:
            A typed DnsRecord instance.
        """
        return DnsRecord(
            id=policy.get("id", ""),
            name=policy.get("domain", ""),
            content=policy.get("ipv4Address", ""),
            type="A",
            ttl=policy.get("ttlSeconds", _DEFAULT_TTL),
            proxied=False,
            # NOTE: zone_id is not returned by UniFi; callers always have it from context.
            zone_id="",
        )
