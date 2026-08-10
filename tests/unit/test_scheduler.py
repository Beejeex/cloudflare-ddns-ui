"""
tests/unit/test_scheduler.py

Unit tests for scheduler.py — job registration and interval rescheduling.
A scheduler instance is created but never started, so no real jobs run.
"""

from __future__ import annotations

from scheduler import _JOB_ID, create_scheduler, reschedule


class _FakeClient:
    """Stand-in for httpx.AsyncClient — registration does not use the client."""


def _make_scheduler(interval_seconds: int = 300):
    return create_scheduler(
        http_client=_FakeClient(),
        unifi_http_client=_FakeClient(),
        interval_seconds=interval_seconds,
    )


def test_create_scheduler_registers_ddns_job():
    """create_scheduler must register the DDNS job with the requested interval."""
    scheduler = _make_scheduler(interval_seconds=123)
    job = scheduler.get_job(_JOB_ID)
    assert job is not None
    assert job.trigger.interval.total_seconds() == 123
    # The job kwargs carry the injected collaborators
    assert job.kwargs["http_client"] is not None
    assert job.kwargs["unifi_http_client"] is not None
    assert job.kwargs["app_state"] is None


def test_reschedule_changes_interval():
    """reschedule must update the DDNS job's trigger interval in place."""
    scheduler = _make_scheduler(interval_seconds=300)
    reschedule(scheduler, _FakeClient(), interval_seconds=60)
    job = scheduler.get_job(_JOB_ID)
    assert job is not None
    assert job.trigger.interval.total_seconds() == 60
