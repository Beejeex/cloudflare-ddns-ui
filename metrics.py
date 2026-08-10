"""
metrics.py

Responsibility: Registers the Prometheus metrics exposed by the application
and renders them in the exposition format for the /metrics endpoint.
Does NOT: contain business logic, HTTP handling, or DB access.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

# A dedicated registry keeps the metric set explicit and lets tests render a
# deterministic payload instead of depending on the process-global registry.
REGISTRY = CollectorRegistry()

# Per-record DNS activity counters.  The "record" label is the FQDN.
ddns_checks_total = Counter(
    "ddns_checks_total",
    "DNS records checked by the DDNS cycle",
    ["record"],
    registry=REGISTRY,
)

ddns_updates_total = Counter(
    "ddns_updates_total",
    "DNS records successfully updated (IP changed)",
    ["record"],
    registry=REGISTRY,
)

ddns_failures_total = Counter(
    "ddns_failures_total",
    "DNS checks or updates that failed",
    ["record"],
    registry=REGISTRY,
)

# Duration of one full DDNS check cycle (Cloudflare pass).
ddns_cycle_duration_seconds = Histogram(
    "ddns_cycle_duration_seconds",
    "Duration of a full DDNS check cycle",
    registry=REGISTRY,
)

# Prometheus exposition content type (from prometheus_client).
CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


def render_metrics() -> bytes:
    """
    Renders all registered metrics in the Prometheus text exposition format.

    Returns:
        The metrics payload as UTF-8 bytes.
    """
    return generate_latest(REGISTRY)
