"""
tests/conftest.py

Shared pytest fixtures used by both unit and integration test suites.
All DB fixtures use in-memory SQLite (StaticPool) and all HTTP fixtures
use respx.mock — no real network calls are made in any test.
"""

from __future__ import annotations

import os

import pytest
import respx
import httpx
from sqlmodel import SQLModel, Session, create_engine
from sqlmodel.pool import StaticPool

# NOTE: Models must be imported before SQLModel.metadata.create_all so that
# all table definitions are registered in the metadata before we call create_all.
import db.models  # noqa: F401 — side-effect import to register table metadata

# ---------------------------------------------------------------------------
# Redirect the DB to /tmp for all test runs — never write to /config/ddns.db
# ---------------------------------------------------------------------------

os.environ.setdefault("DB_PATH", "/tmp/ddns_test.db")


# ---------------------------------------------------------------------------
# Database fixture — in-memory SQLite, isolated per test
# ---------------------------------------------------------------------------


@pytest.fixture(name="db_session")
def db_session_fixture():
    """
    Yields a fresh in-memory SQLite session for each test.

    Tables are created before the test and dropped after, ensuring full
    isolation between tests. Never touches the real /config/ddns.db file.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    SQLModel.metadata.drop_all(engine)


# ---------------------------------------------------------------------------
# HTTP mock fixture — intercepts all httpx calls
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_http():
    """
    Yields a respx router that intercepts all httpx.AsyncClient calls.

    No real network traffic is allowed during tests. Use this fixture
    wherever a service or client would normally make an outbound request.
    """
    with respx.mock(assert_all_called=False) as router:
        yield router


# ---------------------------------------------------------------------------
# Shared httpx.AsyncClient fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
async def http_client():
    """
    Yields a real httpx.AsyncClient instance for use in tests.

    Pair with the mock_http fixture so all requests are intercepted by respx.
    The client is closed after each test.
    """
    async with httpx.AsyncClient() as client:
        yield client
