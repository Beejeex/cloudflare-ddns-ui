"""
tests/unit/test_stats_service.py

Unit tests for services/stats_service.py.
Uses the in-memory SQLite db_session fixture from conftest.py.
"""

from __future__ import annotations

import pytest

from repositories.stats_repository import StatsRepository
from services.stats_service import StatsService


def _make_service(db_session):
    repo = StatsRepository(db_session)
    return StatsService(repo)


_RECORD = "home.example.com"


@pytest.mark.asyncio
async def test_record_checked_creates_entry(db_session):
    """record_checked creates a stats entry and sets last_checked."""
    service = _make_service(db_session)
    stats = await service.record_checked(_RECORD)
    assert stats.record_name == _RECORD
    assert stats.last_checked is not None


@pytest.mark.asyncio
async def test_record_updated_increments_counter(db_session):
    """record_updated increments the update counter."""
    service = _make_service(db_session)
    await service.record_checked(_RECORD)
    stats = await service.record_updated(_RECORD)
    assert stats.updates == 1


@pytest.mark.asyncio
async def test_record_failed_increments_counter(db_session):
    """record_failed increments the failures counter."""
    service = _make_service(db_session)
    await service.record_checked(_RECORD)
    stats = await service.record_failed(_RECORD)
    assert stats.failures == 1


@pytest.mark.asyncio
async def test_get_all_returns_all_records(db_session):
    """get_all returns all tracked records."""
    service = _make_service(db_session)
    await service.record_checked("a.example.com")
    await service.record_checked("b.example.com")
    all_stats = await service.get_all()
    assert len(all_stats) == 2


@pytest.mark.asyncio
async def test_delete_for_record_removes_entry(db_session):
    """delete_for_record removes the stats row."""
    service = _make_service(db_session)
    await service.record_checked(_RECORD)
    deleted = await service.delete_for_record(_RECORD)
    assert deleted is True
    remaining = await service.get_for_record(_RECORD)
    assert remaining is None
