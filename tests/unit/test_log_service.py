"""
tests/unit/test_log_service.py

Unit tests for services/log_service.py.
Uses the in-memory SQLite db_session fixture from conftest.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.log_service import LogService


def _make_service(db_session):
    return LogService(db_session)


@pytest.mark.asyncio
async def test_log_creates_entry(db_session):
    """log() must create a LogEntry row with the correct message and level."""
    service = _make_service(db_session)
    entry = service.log("Test message", level="INFO")

    assert entry.id is not None
    assert entry.message == "Test message"
    assert entry.level == "INFO"


@pytest.mark.asyncio
async def test_log_uppercases_level(db_session):
    """log() must uppercase the level string for consistency."""
    service = _make_service(db_session)
    entry = service.log("warning msg", level="warning")
    assert entry.level == "WARNING"


@pytest.mark.asyncio
async def test_get_recent_returns_newest_first(db_session):
    """get_recent must return entries ordered newest first."""
    service = _make_service(db_session)
    service.log("first")
    service.log("second")
    service.log("third")

    entries = service.get_recent(limit=10)
    assert entries[0].message == "third"
    assert entries[-1].message == "first"


@pytest.mark.asyncio
async def test_get_recent_respects_limit(db_session):
    """get_recent must not return more entries than the specified limit."""
    service = _make_service(db_session)
    for i in range(10):
        service.log(f"msg {i}")

    entries = service.get_recent(limit=3)
    assert len(entries) == 3


@pytest.mark.asyncio
async def test_delete_older_than_removes_old_entries(db_session):
    """delete_older_than must remove entries older than the cutoff."""
    from db.models import LogEntry

    # Manually insert an old entry
    old_entry = LogEntry(
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10),
        level="INFO",
        message="old message",
    )
    db_session.add(old_entry)
    db_session.commit()

    service = _make_service(db_session)
    service.log("recent message")

    deleted = service.delete_older_than(days=7)
    assert deleted == 1

    remaining = service.get_recent(limit=100)
    messages = [e.message for e in remaining]
    assert "old message" not in messages
    assert "recent message" in messages
