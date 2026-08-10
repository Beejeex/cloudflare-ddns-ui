"""
tests/unit/test_metrics.py

Unit tests for metrics.py — verifies the Prometheus exposition payload
contains the expected metric families.
"""

from __future__ import annotations

from metrics import render_metrics


def test_render_metrics_contains_metric_families():
    """The rendered payload must include every registered metric family."""
    payload = render_metrics().decode("utf-8")
    assert "# HELP ddns_checks_total" in payload
    assert "# HELP ddns_updates_total" in payload
    assert "# HELP ddns_failures_total" in payload
    assert "# HELP ddns_cycle_duration_seconds" in payload


def test_render_metrics_has_help_and_type_lines():
    """Each metric must carry HELP and TYPE lines in exposition format."""
    payload = render_metrics().decode("utf-8")
    assert "# TYPE ddns_checks_total counter" in payload
    assert "# TYPE ddns_cycle_duration_seconds histogram" in payload
