"""
cloudflare/unifi_client.py

Responsibility: Implements the DNSProvider protocol using the local UniFi Network
Application REST API (https://{host}/proxy/network/integration/v1). Manages DNS
Policies on a single UniFi site. SSL verification is disabled because UniFi
controllers use self-signed certificates.
Does NOT: read configuration from the database, manage stats, or schedule jobs.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from cloudflare.dns_provider import DnsRecord, DNSProvider
from exceptions import UnifiProviderError

logger = logging.getLogger(__name__)

# Path suffix appended to the controller host to reach the integration API.
# Exported so tests can construct expected URLs without duplicating the string.
_UNIFI_PATH = "/proxy/network/integration/v1"

# Default TTL for newly created DNS policies (4 hours, same as UniFi default)
_DEFAULT_TTL = 14400

# Maximum records to fetch in a single list call (UniFi API max is 200)
_LIST_LIMIT = 200


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

    def __init__(self, http_client: httpx.AsyncClient, api_key: str, host: str) -> None:
        """
        Initialises the client with an HTTP client, API key, and controller host.

        Args:
            http_client: A long-lived httpx.AsyncClient instance (must have verify=False).
            api_key: A UniFi API key with DNS write access.
            host: Hostname or IP of the local UniFi Network Application,
                  e.g. "192.168.1.1" or "unifi.local".
        """
        self._client = http_client
        # Build the base URL once; strip trailing slash to avoid double-slash URLs.
        self._base = f"https://{host.rstrip('/')}{_UNIFI_PATH}"
        self._headers = {
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

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
        url = f"{self._base}/sites/{zone_id}/dns-policies/{record.id}"
        payload: dict[str, Any] = {
            "type": "A_RECORD",
            "enabled": True,
            "domain": record.name,
            "ipv4Address": new_ip,
            "ttlSeconds": record.ttl if record.ttl else _DEFAULT_TTL,
        }
        logger.debug("PUT %s payload=%s", url, payload)
        data = await self._request("PUT", url, json=payload)
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
        url = f"{self._base}/sites/{zone_id}/dns-policies"
        payload: dict[str, Any] = {
            "type": "A_RECORD",
            "enabled": True,
            "domain": record_name,
            "ipv4Address": ip,
            "ttlSeconds": _DEFAULT_TTL,
        }
        logger.debug("POST %s payload=%s", url, payload)
        data = await self._request("POST", url, json=payload)
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
        url = f"{self._base}/sites/{zone_id}/dns-policies/{record_id}"
        logger.debug("DELETE %s", url)
        await self._request("DELETE", url)

    async def list_records(self, zone_id: str) -> list[DnsRecord]:
        """
        Returns all A-record DNS policies on the given site.

        Fetches up to 200 policies (UniFi API maximum per page). Filters to
        A_RECORD type only.

        Args:
            zone_id: The UniFi site UUID.

        Returns:
            A list of DnsRecord instances, possibly empty.

        Raises:
            UnifiProviderError: If the API call fails.
        """
        url = f"{self._base}/sites/{zone_id}/dns-policies"
        params = {"limit": _LIST_LIMIT, "offset": 0}
        logger.debug("GET %s params=%s", url, params)
        data = await self._request("GET", url, params=params)

        records: list[DnsRecord] = []
        for policy in data.get("data", []):
            # NOTE: Only A_RECORD type is relevant — skip CNAME, MX, etc.
            if policy.get("type") == "A_RECORD":
                records.append(self._parse_policy(policy))
        return records

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

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
        try:
            response = await self._client.request(
                method,
                url,
                headers=self._headers,
                params=params,
                json=json,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise UnifiProviderError(
                f"UniFi API {exc.response.status_code} for {method} {url}: "
                f"{exc.response.text[:200]}"
            ) from exc
        except httpx.RequestError as exc:
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
