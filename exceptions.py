"""
exceptions.py

Responsibility: Defines all custom exception classes used across the application.
Does NOT: contain business logic, logging, or HTTP handling.
"""

from __future__ import annotations


class IpFetchError(Exception):
    """
    Raised by IpService when the public IP address cannot be determined.

    This may occur due to network connectivity issues or an unexpected
    response from the upstream IP provider (e.g. api.ipify.org).
    """


class DnsProviderError(Exception):
    """
    Raised by any DNSProvider implementation when a DNS API call fails.

    Includes a human-readable message describing the failure. Callers
    (typically DnsService) must catch this and update failure counters.
    """


class ConfigLoadError(Exception):
    """
    Raised by ConfigRepository when the configuration row is missing or
    cannot be parsed from the database.
    """
