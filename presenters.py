"""
presenters.py

Responsibility: Builds the uniform record-row dict consumed by the record
templates (records_table.html / dashboard.html) from raw DnsRecord, stats,
and per-record config objects.
Does NOT: fetch data, make HTTP calls, or access the database.
"""

from __future__ import annotations

from typing import Any

from cloudflare.dns_provider import DnsRecord
from db.models import RecordConfig, RecordStats


def build_record_row(
    name: str,
    *,
    dns_record: DnsRecord | None = None,
    current_ip: str | None = None,
    stats: RecordStats | None = None,
    cfg: RecordConfig | None = None,
    unifi_policy: DnsRecord | None = None,
    unifi_local_policy: DnsRecord | None = None,
    k8s_namespace: str | None = None,
    k8s_ingress_name: str | None = None,
    live: bool = False,
) -> dict[str, Any]:
    """
    Builds a single record-row dict in the exact shape the templates expect.

    When ``live`` is False (used by action handlers that cannot make CF/UniFi
    calls mid-action) the DNS/IP fields are left as placeholders. When True,
    the up-to-date state is evaluated against the current IP (or the record's
    static IP when ``ip_mode="static"``).

    Args:
        name: The managed FQDN.
        dns_record: The live DNS record from the provider (live mode only).
        current_ip: The host's public IP (live mode only).
        stats: The RecordStats row, or None for a fresh record.
        cfg: The RecordConfig row, or None for defaults.
        unifi_policy: The matched UniFi DNS policy (live mode only).
        unifi_local_policy: The matched ".local" UniFi policy (live mode only).
        k8s_namespace: Optional Kubernetes namespace of a matching Ingress.
        k8s_ingress_name: Optional Kubernetes Ingress name of a matching Ingress.
        live: When True evaluate live DNS/IP state; otherwise emit placeholders.

    Returns:
        A dict of record fields matching the template contract.
    """
    if live:
        dns_ip = dns_record.content if dns_record else "Not Found"
        cf_enabled = cfg.cf_enabled if cfg else True
        if not cf_enabled:
            # NOTE: If CF is disabled the record may not exist in Cloudflare by
            # design — show Unknown, not "Needs update", to avoid misleading.
            is_up_to_date = None
        else:
            # NOTE: Static-IP records are judged against their configured static
            # IP — never against the detected public IP.
            expected_ip = cfg.static_ip if (cfg and cfg.ip_mode == "static" and cfg.static_ip) else current_ip
            is_up_to_date = dns_record is not None and dns_ip == expected_ip
        cf_record_id = dns_record.id if dns_record else None
    else:
        dns_ip = "\u2014"
        is_up_to_date = None
        cf_record_id = None

    return {
        "name": name,
        "cf_record_id": cf_record_id,
        "dns_ip": dns_ip,
        "is_up_to_date": is_up_to_date,
        "updates": stats.updates if stats else 0,
        "failures": stats.failures if stats else 0,
        "last_checked": stats.last_checked.isoformat() if stats and stats.last_checked else None,
        "last_updated": stats.last_updated.isoformat() if stats and stats.last_updated else None,
        "unifi_ip": unifi_policy.content if unifi_policy else None,
        "unifi_local_ip": unifi_local_policy.content if unifi_local_policy else None,
        "unifi_record_id": unifi_policy.id if unifi_policy else None,
        "cfg_cf_enabled": cfg.cf_enabled if cfg else True,
        "cfg_ip_mode": cfg.ip_mode if cfg else "dynamic",
        "cfg_static_ip": cfg.static_ip if cfg else "",
        "cfg_unifi_enabled": cfg.unifi_enabled if cfg else False,
        "cfg_unifi_static_ip": cfg.unifi_static_ip if cfg else "",
        "cfg_unifi_local_enabled": cfg.unifi_local_enabled if cfg else False,
        "cfg_unifi_local_static_ip": cfg.unifi_local_static_ip if cfg else "",
        "k8s_namespace": k8s_namespace,
        "k8s_ingress_name": k8s_ingress_name,
    }
