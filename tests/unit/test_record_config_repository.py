"""
tests/unit/test_record_config_repository.py

Unit tests for repositories/record_config_repository.py, focused on the
bulk flag operations used by the dashboard bulk actions.
"""

from __future__ import annotations

import pytest

from db.models import RecordConfig
from repositories.record_config_repository import RecordConfigRepository


def _seed(db_session, rows: dict[str, RecordConfig]) -> None:
    """Persists the given RecordConfig rows in one commit."""
    for row in rows.values():
        db_session.add(row)
    db_session.commit()


def test_set_cf_enabled_all_enables_existing_rows(db_session):
    """set_cf_enabled_all must flip cf_enabled on existing rows in one commit."""
    repo = RecordConfigRepository(db_session)
    _seed(db_session, {
        "a.example.com": RecordConfig(record_name="a.example.com", cf_enabled=False),
        "b.example.com": RecordConfig(record_name="b.example.com", cf_enabled=False),
    })

    count = repo.set_cf_enabled_all(["a.example.com", "b.example.com"], enabled=True)

    assert count == 2
    assert repo.get("a.example.com").cf_enabled is True
    assert repo.get("b.example.com").cf_enabled is True


def test_set_cf_enabled_all_creates_missing_rows(db_session):
    """set_cf_enabled_all must upsert rows for records with no config yet."""
    repo = RecordConfigRepository(db_session)

    count = repo.set_cf_enabled_all(["new.example.com"], enabled=True)

    assert count == 1
    cfg = repo.get("new.example.com")
    assert cfg.cf_enabled is True


def test_set_cf_enabled_all_empty_list_is_noop(db_session):
    """set_cf_enabled_all with no records must return 0 without committing."""
    repo = RecordConfigRepository(db_session)
    assert repo.set_cf_enabled_all([], enabled=True) == 0


def test_set_unifi_enabled_all_disables_local_companion(db_session):
    """Disabling UniFi must also clear unifi_local_enabled on every record."""
    repo = RecordConfigRepository(db_session)
    _seed(db_session, {
        "a.example.com": RecordConfig(
            record_name="a.example.com",
            unifi_enabled=True,
            unifi_local_enabled=True,
        ),
    })

    count = repo.set_unifi_enabled_all(["a.example.com"], enabled=False)

    assert count == 1
    cfg = repo.get("a.example.com")
    assert cfg.unifi_enabled is False
    assert cfg.unifi_local_enabled is False


def test_set_unifi_enabled_all_enable_keeps_local_unchanged(db_session):
    """Enabling UniFi must not touch the unifi_local_enabled flag."""
    repo = RecordConfigRepository(db_session)
    _seed(db_session, {
        "a.example.com": RecordConfig(
            record_name="a.example.com",
            unifi_enabled=False,
            unifi_local_enabled=False,
        ),
    })

    repo.set_unifi_enabled_all(["a.example.com"], enabled=True)

    cfg = repo.get("a.example.com")
    assert cfg.unifi_enabled is True
    assert cfg.unifi_local_enabled is False


def test_set_flag_all_rejects_unknown_flag(db_session):
    """set_flag_all must reject field names it does not know."""
    repo = RecordConfigRepository(db_session)
    with pytest.raises(ValueError):
        repo.set_flag_all(["a.example.com"], "not_a_flag", True)
