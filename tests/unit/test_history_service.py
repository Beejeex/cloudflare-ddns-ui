"""
tests/unit/test_history_service.py

Unit tests for the per-record IP change history service + repository.
Uses the in-memory SQLite fixture — no real database.
"""

from __future__ import annotations

import pytest

from repositories.history_repository import HistoryRepository
from services.history_service import HistoryService


def _make_service(db_session) -> HistoryService:
    return HistoryService(HistoryRepository(db_session))


def test_record_ip_change_creates_entry(db_session):
    """record_ip_change must persist an entry with source and IP."""
    service = _make_service(db_session)
    entry = service.record_ip_change("home.example.com", "1.2.3.4", source="scheduler")

    assert entry.record_name == "home.example.com"
    assert entry.ip == "1.2.3.4"
    assert entry.source == "scheduler"
    assert entry.timestamp is not None


def test_get_history_returns_newest_first(db_session):
    """get_history must return entries ordered by timestamp descending."""
    service = _make_service(db_session)
    service.record_ip_change("home.example.com", "1.1.1.1")
    service.record_ip_change("home.example.com", "2.2.2.2")

    history = service.get_history("home.example.com")
    assert [h.ip for h in history] == ["2.2.2.2", "1.1.1.1"]


def test_get_history_scoped_to_record(db_session):
    """History for one record must not include another record's entries."""
    service = _make_service(db_session)
    service.record_ip_change("a.example.com", "1.1.1.1")
    service.record_ip_change("b.example.com", "2.2.2.2")

    assert [h.ip for h in service.get_history("a.example.com")] == ["1.1.1.1"]
    assert [h.ip for h in service.get_history("b.example.com")] == ["2.2.2.2"]


def test_get_history_empty_for_unknown_record(db_session):
    """An unknown record returns an empty list."""
    service = _make_service(db_session)
    assert service.get_history("missing.example.com") == []


def test_get_history_respects_limit(db_session):
    """get_history must cap the number of returned entries."""
    service = _make_service(db_session)
    for i in range(10):
        service.record_ip_change("home.example.com", f"10.0.0.{i}")

    history = service.get_history("home.example.com", limit=3)
    assert len(history) == 3


def test_delete_for_record_purges_entries(db_session):
    """delete_for_record must remove all history rows for the record."""
    service = _make_service(db_session)
    service.record_ip_change("a.example.com", "1.1.1.1")
    service.record_ip_change("a.example.com", "2.2.2.2")
    service.record_ip_change("b.example.com", "3.3.3.3")

    assert service.delete_for_record("a.example.com") == 2
    assert service.get_history("a.example.com") == []
    assert len(service.get_history("b.example.com")) == 1
