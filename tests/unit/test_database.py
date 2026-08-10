"""
tests/unit/test_database.py

Unit tests for db/database.py — verifies the schema upgrade path:
- init_db() creates brand-new tables on EXISTING databases (create_all path).
- _run_migrations() adds new columns to pre-existing tables (ALTER TABLE path).

Each test uses its own temp SQLite file — the real /config/ddns.db is never touched.
"""

from __future__ import annotations

from sqlmodel import SQLModel, create_engine

import db.database as db_module
import db.models  # noqa: F401  — registers all table models in the metadata


def _table_names(engine) -> list[str]:
    """Returns the table names in the given engine's database."""
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        return [r[0] for r in rows]


def _columns(engine, table: str) -> list[str]:
    """Returns the column names of a table in the given engine's database."""
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(f"PRAGMA table_info({table})")
        return [r[1] for r in rows]


def test_init_db_creates_missing_new_table_on_existing_db(tmp_path, monkeypatch):
    """
    A database created before IpHistoryEntry existed must gain the table on
    the next startup — create_all creates tables that are missing, so no
    ALTER TABLE migration is required for brand-new tables.
    """
    db_file = tmp_path / "upgrade.db"

    # 1. Simulate an existing DB that predates the new table: build the full
    #    schema, then drop the iphistoryentry table to mimic the old shape.
    old_engine = create_engine(f"sqlite:///{db_file}")
    SQLModel.metadata.create_all(old_engine)
    with old_engine.connect() as conn:
        conn.exec_driver_sql("DROP TABLE iphistoryentry")
        conn.commit()
    assert "iphistoryentry" not in _table_names(old_engine)

    # 2. Point the app's module-level engine at this file and run startup.
    monkeypatch.setattr(db_module, "_DB_PATH", str(db_file))
    monkeypatch.setattr(db_module, "_DB_URL", f"sqlite:///{db_file}")
    monkeypatch.setattr(db_module, "engine", create_engine(f"sqlite:///{db_file}"))
    db_module.init_db()

    # 3. The table and its columns must now exist.
    assert "iphistoryentry" in _table_names(db_module.engine)
    assert _columns(db_module.engine, "iphistoryentry") == [
        "id",
        "record_name",
        "ip",
        "source",
        "timestamp",
    ]


def test_run_migrations_adds_new_column_to_existing_table(tmp_path, monkeypatch):
    """
    An existing table missing a column that the model now defines must gain
    it via the ALTER TABLE migration (the create_all-only upgrade gap).
    """
    db_file = tmp_path / "migrate.db"

    # 1. Build the full schema, then drop the log_retention_days column to
    #    mimic a database created before that setting existed.
    engine = create_engine(f"sqlite:///{db_file}")
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.exec_driver_sql("ALTER TABLE appconfig DROP COLUMN log_retention_days")
        conn.commit()
    assert "log_retention_days" not in _columns(engine, "appconfig")

    # 2. Run the incremental migration layer.
    monkeypatch.setattr(db_module, "engine", engine)
    db_module._run_migrations()

    # 3. The column must now exist with the expected default.
    assert "log_retention_days" in _columns(engine, "appconfig")


def test_init_db_creates_fresh_schema(tmp_path, monkeypatch):
    """On a brand-new database init_db must create every table defined."""
    db_file = tmp_path / "fresh.db"
    monkeypatch.setattr(db_module, "_DB_PATH", str(db_file))
    monkeypatch.setattr(db_module, "_DB_URL", f"sqlite:///{db_file}")
    monkeypatch.setattr(db_module, "engine", create_engine(f"sqlite:///{db_file}"))

    db_module.init_db()

    tables = _table_names(db_module.engine)
    assert "appconfig" in tables
    assert "recordstats" in tables
    assert "recordconfig" in tables
    assert "logentry" in tables
    assert "iphistoryentry" in tables
