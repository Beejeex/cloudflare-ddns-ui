"""
tests/unit/test_utils.py

Unit tests for utils.py helper functions.
"""

from __future__ import annotations

from utils import (
    cache_invalidate_prefix,
    cache_read,
    cache_write,
    mask_secret,
    to_local_policy_name,
)


def test_mask_secret_empty_string():
    """An empty secret must mask to an empty string."""
    assert mask_secret("") == ""


def test_mask_secret_shows_last_four():
    """A long secret must show only the last four characters."""
    assert mask_secret("abcdefgh1234") == "\u2022" * 8 + "1234"


def test_mask_secret_fully_masks_short_secret():
    """A secret of four characters or fewer must be fully masked."""
    assert mask_secret("abcd") == "\u2022" * 4


def test_mask_secret_never_leaks_full_value():
    """The masked form must never contain the full original secret."""
    secret = "super-secret-token-xyz"
    masked = mask_secret(secret)
    assert secret not in masked
    assert masked.endswith("xyz")


def test_to_local_policy_name_replaces_tld():
    """to_local_policy_name replaces only the TLD with 'local'."""
    assert to_local_policy_name("home.example.net") == "home.example.local"


# ---------------------------------------------------------------------------
# TTL cache helpers
# ---------------------------------------------------------------------------


def test_cache_write_then_read_returns_value():
    """A freshly written entry must be readable within its TTL."""
    cache: dict = {}
    cache_write(cache, "list_records:zone1", ["a", "b"])
    assert cache_read(cache, "list_records:zone1", ttl=30.0) == ["a", "b"]


def test_cache_read_missing_key_returns_none():
    """cache_read must return None for an absent key."""
    assert cache_read({}, "missing", ttl=30.0) is None


def test_cache_read_none_cache_returns_none():
    """Caching disabled (cache=None) must always return None."""
    assert cache_read(None, "k", ttl=30.0) is None
    cache_write(None, "k", "v")  # must not raise


def test_cache_read_expired_returns_none():
    """An entry older than the TTL must be treated as a miss."""
    cache: dict = {}
    cache_write(cache, "k", "stale")
    # Rewrite the entry with an ancient timestamp to simulate expiry.
    cache["k"] = (0.0, "stale")
    assert cache_read(cache, "k", ttl=30.0) is None


def test_cache_invalidate_prefix_removes_matching_keys():
    """cache_invalidate_prefix must drop only keys with the matching prefix."""
    cache: dict = {}
    cache_write(cache, "list_records:zone1", [1])
    cache_write(cache, "list_records:zone2", [2])
    cache_write(cache, "list_sites", [3])
    cache_invalidate_prefix(cache, "list_records:zone1")
    assert "list_records:zone1" not in cache
    assert "list_records:zone2" in cache
    assert "list_sites" in cache
