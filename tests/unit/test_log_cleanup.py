"""
tests/unit/test_log_cleanup.py

Unit tests for log_cleanup.py (cleanup scheduling and log pruning).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import log_cleanup
from services.log_service import LogService
from utils import utcnow_naive


@pytest.fixture(autouse=True)
def _reset_cleanup_state():
    """Ensure the in-memory last-run marker starts fresh for every test."""
    log_cleanup._last_cleanup_at = None
    yield
    log_cleanup._last_cleanup_at = None


# ---------------------------------------------------------------------------
# should_run_cleanup
# ---------------------------------------------------------------------------


def test_should_run_cleanup_true_initially():
    """With no recorded run, cleanup is due immediately."""
    log_cleanup._last_cleanup_at = None
    assert log_cleanup.should_run_cleanup() is True


def test_should_run_cleanup_false_after_recent_run():
    """A run less than 24h ago means cleanup is not due."""
    log_cleanup._last_cleanup_at = datetime.now(timezone.utc)
    assert log_cleanup.should_run_cleanup() is False


def test_should_run_cleanup_true_after_24h():
    """A run more than 24h ago means cleanup is due again."""
    log_cleanup._last_cleanup_at = datetime.now(timezone.utc) - timedelta(hours=25)
    assert log_cleanup.should_run_cleanup() is True


# ---------------------------------------------------------------------------
# run_cleanup
# ---------------------------------------------------------------------------


def test_run_cleanup_deletes_only_old_entries(db_session):
    """run_cleanup must delete entries older than days_to_keep and keep newer ones."""
    service = LogService(db_session)

    # Backdate an entry to 10 days ago (older than the 7-day retention)
    old = service.log("old entry", level="INFO")
    old.timestamp = utcnow_naive() - timedelta(days=10)
    db_session.add(old)
    db_session.commit()

    service.log("fresh entry", level="INFO")

    deleted = log_cleanup.run_cleanup(db_session, days_to_keep=7)
    assert deleted == 1

    messages = [e.message for e in service.get_recent(limit=100)]
    assert "fresh entry" in messages
    assert "old entry" not in messages


def test_run_cleanup_noop_when_not_due(db_session):
    """A second run within 24h must be a no-op."""
    log_cleanup._last_cleanup_at = datetime.now(timezone.utc)
    service = LogService(db_session)
    service.log("entry", level="INFO")

    deleted = log_cleanup.run_cleanup(db_session, days_to_keep=7)
    assert deleted == 0
