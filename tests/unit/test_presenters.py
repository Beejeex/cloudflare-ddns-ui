"""
tests/unit/test_presenters.py

Unit tests for presenters.build_record_row — the shared record-row shape.
"""

from __future__ import annotations

from cloudflare.dns_provider import DnsRecord
from db.models import RecordConfig, RecordStats
from presenters import build_record_row
from utils import utcnow_naive


def _record(name: str = "home.example.com", content: str = "1.2.3.4") -> DnsRecord:
    return DnsRecord(
        id="rec1", name=name, content=content, type="A", ttl=1, proxied=False, zone_id="z1"
    )


def test_non_live_row_uses_placeholders():
    """Without live mode the DNS/IP fields must be placeholders."""
    row = build_record_row("home.example.com")
    assert row["name"] == "home.example.com"
    assert row["dns_ip"] == "\u2014"
    assert row["is_up_to_date"] is None
    assert row["cf_record_id"] is None
    assert row["updates"] == 0
    assert row["failures"] == 0
    assert row["cfg_cf_enabled"] is True
    assert row["k8s_namespace"] is None


def test_live_row_up_to_date_dynamic():
    """A dynamic-mode record matching the public IP must be up to date."""
    stats = RecordStats(
        record_name="home.example.com",
        updates=3,
        failures=1,
        last_checked=utcnow_naive(),
    )
    cfg = RecordConfig(record_name="home.example.com", cf_enabled=True)
    row = build_record_row(
        "home.example.com",
        dns_record=_record(),
        current_ip="1.2.3.4",
        stats=stats,
        cfg=cfg,
        live=True,
    )
    assert row["is_up_to_date"] is True
    assert row["dns_ip"] == "1.2.3.4"
    assert row["cf_record_id"] == "rec1"
    assert row["updates"] == 3
    assert row["failures"] == 1


def test_live_row_static_ip_uses_static_ip():
    """A static-mode record must be judged against its static IP, not the public IP."""
    cfg = RecordConfig(
        record_name="home.example.com", cf_enabled=True, ip_mode="static", static_ip="5.5.5.5"
    )
    row = build_record_row(
        "home.example.com",
        dns_record=_record(content="5.5.5.5"),
        current_ip="9.9.9.9",
        cfg=cfg,
        live=True,
    )
    assert row["is_up_to_date"] is True


def test_live_row_pending_when_ip_differs():
    """A record whose DNS IP differs from the expected IP must be pending."""
    cfg = RecordConfig(record_name="home.example.com", cf_enabled=True)
    row = build_record_row(
        "home.example.com",
        dns_record=_record(content="8.8.8.8"),
        current_ip="1.2.3.4",
        cfg=cfg,
        live=True,
    )
    assert row["is_up_to_date"] is False


def test_live_row_unknown_when_cf_disabled():
    """With Cloudflare disabled the up-to-date state must be unknown, not pending."""
    cfg = RecordConfig(record_name="home.example.com", cf_enabled=False)
    row = build_record_row(
        "home.example.com",
        dns_record=_record(),
        current_ip="1.2.3.4",
        cfg=cfg,
        live=True,
    )
    assert row["is_up_to_date"] is None


def test_live_row_not_found_when_no_record():
    """A live row without a DNS record must show Not Found and be pending."""
    cfg = RecordConfig(record_name="home.example.com", cf_enabled=True)
    row = build_record_row("home.example.com", current_ip="1.2.3.4", cfg=cfg, live=True)
    assert row["dns_ip"] == "Not Found"
    assert row["is_up_to_date"] is False
