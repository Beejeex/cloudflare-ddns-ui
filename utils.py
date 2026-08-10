"""
utils.py

Responsibility: Provides small, pure helper functions shared by multiple
modules across the application.
Does NOT: contain business logic, make HTTP calls, or access the database.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


def utcnow_naive() -> datetime:
    """
    Returns the current UTC time as a naive datetime.

    SQLite stores datetimes without timezone information (SQLAlchemy's
    SQLite dialect strips tzinfo on round-trip), so the codebase stores
    naive-UTC timestamps everywhere. This helper centralises that
    convention and avoids the deprecated ``datetime.utcnow()``.

    Returns:
        A naive datetime representing the current UTC time.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local_policy_name(record_name: str) -> str:
    """
    Converts a managed FQDN into its UniFi local policy name.

    Replaces only the TLD (last label) with "local", preserving all
    intermediate labels so the full subdomain structure is retained.

    Args:
        record_name: Managed DNS name, e.g. "home.example.net".

    Returns:
        Local DNS name, e.g. "home.example.local".
    """
    name = record_name.strip()
    if name.endswith(".local"):
        return name
    # rsplit on the last dot so we keep all intermediate labels intact.
    parts = name.rsplit(".", 1)
    if len(parts) == 1:
        # No dot present — nothing to replace.
        return name
    return f"{parts[0]}.local"


def mask_secret(secret: str) -> str:
    """
    Returns a display-safe masked form of a secret for HTML value attributes.

    Shows the last four characters behind a fixed bullet mask so the user can
    tell a token is configured without exposing the full secret in the DOM
    (defense-in-depth for an internal, trusted-network app).

    Args:
        secret: The raw secret string (API token or key).

    Returns:
        The masked string, or "" if the secret is empty. A secret of four
        characters or fewer is fully masked (no suffix is revealed).
    """
    if not secret:
        return ""
    if len(secret) <= 4:
        return "\u2022" * len(secret)
    return "\u2022" * 8 + secret[-4:]


def cache_read(
    cache: dict[str, tuple[float, Any]] | None,
    key: str,
    ttl: float,
) -> Any | None:
    """
    Returns a cached value if it is still fresh, otherwise None.

    Entries are stored as ``(monotonic_fetch_time, value)`` tuples.  When no
    cache is provided (tests, or callers that must always fetch) this returns
    None immediately so behaviour is unchanged.

    Args:
        cache: The shared cache dict, or None to disable caching.
        key: Stable cache key for the value.
        ttl: Freshness window in seconds.

    Returns:
        The cached value if fresh, else None.
    """
    if cache is None:
        return None
    entry = cache.get(key)
    if entry is None:
        return None
    fetched_at, value = entry
    if time.monotonic() - fetched_at < ttl:
        return value
    return None


def cache_write(
    cache: dict[str, tuple[float, Any]] | None,
    key: str,
    value: Any,
) -> None:
    """
    Stores a value in the cache with the current monotonic timestamp.

    Args:
        cache: The shared cache dict, or None to disable caching.
        key: Stable cache key for the value.
        value: The value to cache.

    Returns:
        None
    """
    if cache is None:
        return
    cache[key] = (time.monotonic(), value)


def cache_invalidate_prefix(
    cache: dict[str, tuple[float, Any]] | None,
    prefix: str,
) -> None:
    """
    Removes every cache entry whose key starts with the given prefix.

    Used by DNS providers to drop a zone's listing after a mutation so the
    next read re-fetches the authoritative state.

    Args:
        cache: The shared cache dict, or None to disable caching.
        prefix: Key prefix to invalidate, e.g. "list_records:zone123".

    Returns:
        None
    """
    if cache is None:
        return
    for key in [k for k in cache if k.startswith(prefix)]:
        del cache[key]
