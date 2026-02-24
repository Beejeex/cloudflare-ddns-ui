"""
tests/unit/test_config_service.py

Unit tests for services/config_service.py.
Uses the in-memory SQLite db_session fixture from conftest.py.
"""

from __future__ import annotations

import pytest

from repositories.config_repository import ConfigRepository
from services.config_service import ConfigService


def _make_service(db_session):
    repo = ConfigRepository(db_session)
    return ConfigService(repo)


@pytest.mark.asyncio
async def test_get_managed_records_empty_on_fresh_db(db_session):
    """get_managed_records returns [] when no records have been added."""
    service = _make_service(db_session)
    records = await service.get_managed_records()
    assert records == []


@pytest.mark.asyncio
async def test_add_managed_record_returns_true_on_success(db_session):
    """add_managed_record returns True when a new record is added."""
    service = _make_service(db_session)
    added = await service.add_managed_record("home.example.com")
    assert added is True


@pytest.mark.asyncio
async def test_add_managed_record_returns_false_on_duplicate(db_session):
    """add_managed_record returns False when the record is already in the list."""
    service = _make_service(db_session)
    await service.add_managed_record("home.example.com")
    added_again = await service.add_managed_record("home.example.com")
    assert added_again is False


@pytest.mark.asyncio
async def test_add_then_get_managed_records(db_session):
    """Adding records must be visible via get_managed_records."""
    service = _make_service(db_session)
    await service.add_managed_record("home.example.com")
    await service.add_managed_record("vpn.example.com")
    records = await service.get_managed_records()
    assert "home.example.com" in records
    assert "vpn.example.com" in records


@pytest.mark.asyncio
async def test_remove_managed_record_returns_true(db_session):
    """remove_managed_record returns True when a record is successfully removed."""
    service = _make_service(db_session)
    await service.add_managed_record("home.example.com")
    removed = await service.remove_managed_record("home.example.com")
    assert removed is True


@pytest.mark.asyncio
async def test_remove_managed_record_returns_false_when_not_present(db_session):
    """remove_managed_record returns False when the record is not in the list."""
    service = _make_service(db_session)
    removed = await service.remove_managed_record("nonexistent.example.com")
    assert removed is False


@pytest.mark.asyncio
async def test_update_credentials_persists(db_session):
    """update_credentials persists the API token and zones."""
    service = _make_service(db_session)
    await service.update_credentials(
        api_token="tok123",
        zones={"example.com": "zone-abc"},
        refresh=60,
        interval=600,
    )
    token = await service.get_api_token()
    zones = await service.get_zones()
    assert token == "tok123"
    assert zones["example.com"] == "zone-abc"
