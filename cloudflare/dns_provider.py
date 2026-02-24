"""
cloudflare/dns_provider.py

Responsibility: Defines the DNSProvider Protocol and the DnsRecord value object.
Does NOT: make HTTP calls, access the database, or implement any provider logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Value object — stable shape returned by all DNSProvider implementations
# ---------------------------------------------------------------------------


@dataclass
class DnsRecord:
    """
    Represents a single DNS A-record as returned by a DNSProvider.

    Using a dataclass (not a raw dict) ensures all callers receive a
    consistent, typed shape regardless of which provider is active.
    """

    # Provider-assigned unique identifier for the record
    id: str

    # Fully-qualified DNS name, e.g. "home.example.com"
    name: str

    # Current IP address stored in the record
    content: str

    # Record type; this application only manages "A" records
    type: str

    # TTL in seconds; 1 means "automatic" on Cloudflare
    ttl: int

    # Whether the record is proxied through the provider's CDN
    proxied: bool

    # The zone ID to which this record belongs
    zone_id: str


# ---------------------------------------------------------------------------
# Abstract interface — all DNS providers must implement this contract
# ---------------------------------------------------------------------------


@runtime_checkable
class DNSProvider(Protocol):
    """
    Abstract protocol for DNS record management.

    All DNS provider implementations (Cloudflare, Kubernetes Ingress,
    UniFi Network API, etc.) must satisfy this interface. DnsService
    depends on this abstraction, never on a concrete implementation.

    Adding a new provider means implementing this protocol — no changes
    to DnsService, routes, or the scheduler are required (OCP).
    """

    async def get_record(self, zone_id: str, record_name: str) -> DnsRecord | None:
        """
        Fetches a single A-record by name within the given zone.

        Args:
            zone_id: The provider-assigned zone identifier.
            record_name: The fully-qualified DNS name to look up.

        Returns:
            A DnsRecord if the record exists, or None if not found.

        Raises:
            DnsProviderError: If the API call fails.
        """
        ...

    async def update_record(self, zone_id: str, record: DnsRecord, new_ip: str) -> DnsRecord:
        """
        Updates an existing A-record with a new IP address.

        Args:
            zone_id: The provider-assigned zone identifier.
            record: The existing DnsRecord to update.
            new_ip: The new IPv4 address to write.

        Returns:
            The updated DnsRecord as confirmed by the provider.

        Raises:
            DnsProviderError: If the API call fails.
        """
        ...

    async def create_record(self, zone_id: str, record_name: str, ip: str) -> DnsRecord:
        """
        Creates a new A-record in the given zone.

        Args:
            zone_id: The provider-assigned zone identifier.
            record_name: The fully-qualified DNS name for the new record.
            ip: The IPv4 address for the new record.

        Returns:
            The newly created DnsRecord.

        Raises:
            DnsProviderError: If the API call fails.
        """
        ...

    async def delete_record(self, zone_id: str, record_id: str) -> None:
        """
        Deletes a DNS record from the given zone.

        Args:
            zone_id: The provider-assigned zone identifier.
            record_id: The provider-assigned unique identifier of the record.

        Returns:
            None

        Raises:
            DnsProviderError: If the API call fails.
        """
        ...

    async def list_records(self, zone_id: str) -> list[DnsRecord]:
        """
        Returns all A-records in the given zone.

        Args:
            zone_id: The provider-assigned zone identifier.

        Returns:
            A list of DnsRecord instances, possibly empty.

        Raises:
            DnsProviderError: If the API call fails.
        """
        ...
