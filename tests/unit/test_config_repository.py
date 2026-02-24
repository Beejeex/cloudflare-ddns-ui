"""
tests/unit/test_config_repository.py

Unit tests for repositories/config_repository.py.
Uses the in-memory SQLite db_session fixture from conftest.py.
"""

from __future__ import annotations

import json
import pytest

from repositories.config_repository import ConfigRepository


# ---------------------------------------------------------------------------
# load — creates defaults when no row exists
# ---------------------------------------------------------------------------


def test_load_creates_default_row(db_session):
    """load() must seed a default AppConfig row when the table is empty."""
    repo = ConfigRepository(db_session)
    config = repo.load()

    assert config is not None
    assert config.id is not None
    assert config.api_token == ""
    assert config.interval == 300


def test_load_returns_existing_row(db_session):
    """load() must not create a duplicate row on subsequent calls."""
    repo = ConfigRepository(db_session)
    first = repo.load()
    second = repo.load()

    assert first.id == second.id


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


def test_save_persists_changes(db_session):
    """save() must persist mutations so a subsequent load() reads them back."""
    repo = ConfigRepository(db_session)
    config = repo.load()
    config.api_token = "new-token-xyz"
    repo.save(config)

    # Re-load from DB to confirm persistence
    reloaded = repo.load()
    assert reloaded.api_token == "new-token-xyz"


# ---------------------------------------------------------------------------
# JSON field helpers
# ---------------------------------------------------------------------------


def test_get_set_zones(db_session):
    """get/set_zones must round-trip a zones dict through JSON correctly."""
    repo = ConfigRepository(db_session)
    config = repo.load()
    zones = {"example.com": "zone123", "another.com": "zone456"}

    repo.set_zones(config, zones)
    assert repo.get_zones(config) == zones


def test_get_set_records(db_session):
    """get/set_records must round-trip a records list through JSON correctly."""
    repo = ConfigRepository(db_session)
    config = repo.load()
    records = ["home.example.com", "vpn.example.com"]

    repo.set_records(config, records)
    assert repo.get_records(config) == records


def test_get_zones_returns_empty_on_corrupt_json(db_session):
    """get_zones must return {} (not raise) when zones_json is corrupt."""
    repo = ConfigRepository(db_session)
    config = repo.load()
    config.zones_json = "not-valid-json"

    result = repo.get_zones(config)
    assert result == {}
