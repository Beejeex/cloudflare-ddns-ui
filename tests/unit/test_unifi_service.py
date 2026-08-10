"""
tests/unit/test_unifi_service.py

Unit tests for services/unifi_service.py — the UniFi DNS policy sync pass.
Uses a recording fake client so no real controller traffic is involved.
"""

from __future__ import annotations

import pytest

from cloudflare.dns_provider import DnsRecord
from db.models import RecordConfig
from exceptions import UnifiProviderError
from repositories.stats_repository import StatsRepository
from services.log_service import LogService
from services.unifi_service import UniFiService

_SITE = "11111111-0000-0000-0000-000000000001"
_DEFAULT_IP = "172.20.9.100"


class _RecordingUnifiClient:
    """In-memory DNSProvider stand-in that records every mutation."""

    def __init__(self) -> None:
        self.policies: dict[str, DnsRecord] = {}
        self.calls: list[tuple[str, str, str]] = []

    async def list_records(self, site_id: str) -> list[DnsRecord]:
        return list(self.policies.values())

    async def create_record(self, site_id: str, name: str, ip: str) -> DnsRecord:
        rec = DnsRecord(id=f"id-{name}", name=name, content=ip, type="A", ttl=0, proxied=False, zone_id=site_id)
        self.policies[name] = rec
        self.calls.append(("create", name, ip))
        return rec

    async def update_record(self, site_id: str, record: DnsRecord, ip: str) -> DnsRecord:
        rec = DnsRecord(id=record.id, name=record.name, content=ip, type="A", ttl=0, proxied=False, zone_id=site_id)
        self.policies[record.name] = rec
        self.calls.append(("update", record.name, ip))
        return rec

    async def delete_record(self, site_id: str, record_id: str) -> None:
        self.policies = {k: v for k, v in self.policies.items() if v.id != record_id}
        self.calls.append(("delete", record_id, ""))


class _FailingListClient(_RecordingUnifiClient):
    """Client whose initial list call always fails."""

    async def list_records(self, site_id: str) -> list[DnsRecord]:
        raise UnifiProviderError("controller unreachable")


def _make_service(db_session, client) -> UniFiService:
    return UniFiService(client, LogService(db_session), StatsRepository(db_session))


def _cfg(name: str, **kwargs) -> RecordConfig:
    return RecordConfig(record_name=name, **kwargs)


# ---------------------------------------------------------------------------
# Main domain policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_creates_missing_policy(db_session):
    """A record with unifi_enabled=True must get a policy created."""
    client = _RecordingUnifiClient()
    service = _make_service(db_session, client)
    cfgs = {"home.example.com": _cfg("home.example.com", unifi_enabled=True, unifi_static_ip="192.168.1.10")}

    await service.sync_policies(["home.example.com"], cfgs, site_id=_SITE, default_ip=_DEFAULT_IP)

    assert client.policies["home.example.com"].content == "192.168.1.10"
    assert ("create", "home.example.com", "192.168.1.10") in client.calls
    # last_checked must be stamped on success
    assert StatsRepository(db_session).get_by_name("home.example.com") is not None


@pytest.mark.asyncio
async def test_sync_updates_policy_when_ip_differs(db_session):
    """An existing policy with a different IP must be updated."""
    client = _RecordingUnifiClient()
    client.policies["home.example.com"] = DnsRecord(
        id="p1", name="home.example.com", content="1.1.1.1", type="A", ttl=0, proxied=False, zone_id=_SITE
    )
    service = _make_service(db_session, client)
    cfgs = {"home.example.com": _cfg("home.example.com", unifi_enabled=True, unifi_static_ip="192.168.1.10")}

    await service.sync_policies(["home.example.com"], cfgs, site_id=_SITE, default_ip=_DEFAULT_IP)

    assert client.policies["home.example.com"].content == "192.168.1.10"
    assert ("update", "home.example.com", "192.168.1.10") in client.calls


@pytest.mark.asyncio
async def test_sync_uses_default_ip_when_no_static_ip(db_session):
    """A record without a per-record IP must fall back to the default IP."""
    client = _RecordingUnifiClient()
    service = _make_service(db_session, client)
    cfgs = {"home.example.com": _cfg("home.example.com", unifi_enabled=True)}

    await service.sync_policies(["home.example.com"], cfgs, site_id=_SITE, default_ip=_DEFAULT_IP)

    assert client.policies["home.example.com"].content == _DEFAULT_IP


@pytest.mark.asyncio
async def test_sync_skips_when_no_ip_configured(db_session):
    """A record with no per-record IP and no default must be skipped (not created)."""
    client = _RecordingUnifiClient()
    service = _make_service(db_session, client)
    cfgs = {"home.example.com": _cfg("home.example.com", unifi_enabled=True)}

    await service.sync_policies(["home.example.com"], cfgs, site_id=_SITE, default_ip="")

    assert "home.example.com" not in client.policies


@pytest.mark.asyncio
async def test_sync_deletes_policy_when_disabled(db_session):
    """A record with unifi_enabled=False must have its existing policy removed."""
    client = _RecordingUnifiClient()
    client.policies["home.example.com"] = DnsRecord(
        id="p1", name="home.example.com", content="1.1.1.1", type="A", ttl=0, proxied=False, zone_id=_SITE
    )
    service = _make_service(db_session, client)
    cfgs = {"home.example.com": _cfg("home.example.com", unifi_enabled=False)}

    await service.sync_policies(["home.example.com"], cfgs, site_id=_SITE, default_ip=_DEFAULT_IP)

    assert "home.example.com" not in client.policies


# ---------------------------------------------------------------------------
# .local companion policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_creates_local_policy(db_session):
    """unifi_local_enabled=True must create the ".local" companion policy."""
    client = _RecordingUnifiClient()
    service = _make_service(db_session, client)
    cfgs = {"home.example.com": _cfg("home.example.com", unifi_local_enabled=True)}

    await service.sync_policies(["home.example.com"], cfgs, site_id=_SITE, default_ip=_DEFAULT_IP)

    assert client.policies["home.example.local"].content == _DEFAULT_IP


@pytest.mark.asyncio
async def test_sync_local_only_does_not_touch_main_policy(db_session):
    """A .local-only setup must not create the main domain policy."""
    client = _RecordingUnifiClient()
    service = _make_service(db_session, client)
    cfgs = {"home.example.com": _cfg("home.example.com", unifi_local_enabled=True)}

    await service.sync_policies(["home.example.com"], cfgs, site_id=_SITE, default_ip=_DEFAULT_IP)

    assert "home.example.local" in client.policies
    assert "home.example.com" not in client.policies


@pytest.mark.asyncio
async def test_sync_skips_local_when_record_is_local(db_session):
    """A record that already ends in .local has no separate companion to manage."""
    client = _RecordingUnifiClient()
    service = _make_service(db_session, client)
    cfgs = {"home.local": _cfg("home.local", unifi_local_enabled=True)}

    await service.sync_policies(["home.local"], cfgs, site_id=_SITE, default_ip=_DEFAULT_IP)

    assert client.policies == {}


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_aborts_when_list_fails(db_session):
    """A failed policy listing must abort the pass before any mutation."""
    client = _FailingListClient()
    service = _make_service(db_session, client)
    cfgs = {"home.example.com": _cfg("home.example.com", unifi_enabled=True, unifi_static_ip="1.1.1.1")}

    await service.sync_policies(["home.example.com"], cfgs, site_id=_SITE, default_ip=_DEFAULT_IP)

    assert client.calls == []
