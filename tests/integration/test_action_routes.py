"""
tests/integration/test_action_routes.py

Integration tests for routes/action_routes.py.
Uses FastAPI's TestClient as a context manager so the lifespan starts and stops
cleanly for each test. Depends() providers are overridden with test doubles
backed by the in-memory SQLite fixture.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import app
from db.models import RecordConfig
from dependencies import (
    get_config_service,
    get_log_service,
    get_stats_service,
)
from repositories.config_repository import ConfigRepository
from repositories.record_config_repository import RecordConfigRepository
from repositories.stats_repository import StatsRepository
from services.config_service import ConfigService
from services.log_service import LogService
from services.stats_service import StatsService


def _apply_overrides(db_session: Session) -> None:
    """Install dependency overrides backed by the test DB session."""
    config_repo = ConfigRepository(db_session)
    stats_repo = StatsRepository(db_session)
    app.dependency_overrides[get_config_service] = lambda: ConfigService(config_repo)
    app.dependency_overrides[get_stats_service] = lambda: StatsService(stats_repo)
    app.dependency_overrides[get_log_service] = lambda: LogService(db_session)


# ---------------------------------------------------------------------------
# POST /update-config — masked-secret handling
# ---------------------------------------------------------------------------


def test_update_config_keeps_stored_token_when_masked(db_session):
    """
    POST /update-config must not overwrite the stored token with the masked
    placeholder value shown in the form.
    """
    import asyncio

    from utils import mask_secret

    async def _scenario() -> None:
        repo = ConfigRepository(db_session)
        service = ConfigService(repo)
        # Seed a real token, then submit its masked form back via the form.
        await service.update_credentials(
            api_token="real-token-abc12345",
            zones={"example.com": "zone1"},
            refresh=30,
            interval=300,
        )
        _apply_overrides(db_session)

        with TestClient(app, raise_server_exceptions=False) as client:
            client.post(
                "/update-config",
                data={
                    "api_token": mask_secret("real-token-abc12345"),
                    "zones": '{"example.com": "zone1"}',
                    "refresh": "30",
                    "interval": "300",
                    "unifi_api_key": "",
                },
            )
        app.dependency_overrides.clear()

        # The stored token must be unchanged (not replaced by the bullet mask).
        assert (await service.get_config()).api_token == "real-token-abc12345"

    asyncio.run(_scenario())


def test_update_config_replaces_token_when_new_value(db_session):
    """A genuinely new token pasted into the form must replace the stored one."""
    import asyncio

    async def _scenario() -> None:
        repo = ConfigRepository(db_session)
        service = ConfigService(repo)
        await service.update_credentials(
            api_token="old-token-9999",
            zones={"example.com": "zone1"},
            refresh=30,
            interval=300,
        )
        _apply_overrides(db_session)

        with TestClient(app, raise_server_exceptions=False) as client:
            client.post(
                "/update-config",
                data={
                    "api_token": "brand-new-token-1234",
                    "zones": '{"example.com": "zone1"}',
                    "refresh": "30",
                    "interval": "300",
                    "unifi_api_key": "",
                },
            )
        app.dependency_overrides.clear()

        assert (await service.get_config()).api_token == "brand-new-token-1234"

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# POST /add-to-managed
# ---------------------------------------------------------------------------


def test_add_to_managed_returns_html_fragment(db_session):
    """POST /add-to-managed must return HTML (not a redirect)."""
    _apply_overrides(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/add-to-managed", data={"record_name": "home.example.com"})
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "home.example.com" in response.text


def test_add_to_managed_persists_record(db_session):
    """POST /add-to-managed must persist the new record to the DB."""
    _apply_overrides(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/add-to-managed", data={"record_name": "vpn.example.com"})
    app.dependency_overrides.clear()

    repo = ConfigRepository(db_session)
    config = repo.load()
    records = repo.get_records(config)
    assert "vpn.example.com" in records


# ---------------------------------------------------------------------------
# POST /remove-from-managed
# ---------------------------------------------------------------------------


def test_remove_from_managed_removes_record(db_session):
    """POST /remove-from-managed must remove the record from the DB."""
    # Seed a record first
    repo = ConfigRepository(db_session)
    config = repo.load()
    repo.set_records(config, ["home.example.com"])
    repo.save(config)

    _apply_overrides(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/remove-from-managed", data={"record_name": "home.example.com"})
    app.dependency_overrides.clear()

    assert response.status_code == 200
    # After removal, the table fragment should not contain the record
    updated_records = repo.get_records(repo.load())
    assert "home.example.com" not in updated_records


# ---------------------------------------------------------------------------
# POST /clear-logs
# ---------------------------------------------------------------------------


def test_clear_logs_returns_html_fragment(db_session):
    """POST /clear-logs must return an HTML fragment, not a redirect."""
    _apply_overrides(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/clear-logs")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# POST /reset-updates
# ---------------------------------------------------------------------------


def test_reset_updates_zeroes_counter(db_session):
    """POST /reset-updates must reset the updates counter to zero and return HTML."""
    repo = StatsRepository(db_session)
    repo.record_update("home.example.com")
    repo.record_update("home.example.com")
    config_repo = ConfigRepository(db_session)
    config = config_repo.load()
    config_repo.set_records(config, ["home.example.com"])
    config_repo.save(config)

    _apply_overrides(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/reset-updates", data={"record_name": "home.example.com"})
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    stats = repo.get_by_name("home.example.com")
    assert stats.updates == 0


# ---------------------------------------------------------------------------
# POST /bulk-set-cf and /bulk-set-unifi
# ---------------------------------------------------------------------------


def test_bulk_set_cf_enables_all_records(db_session):
    """POST /bulk-set-cf enabled=true must flip cf_enabled on every record."""
    from dependencies import get_record_config_repo

    config_repo = ConfigRepository(db_session)
    config = config_repo.load()
    config_repo.set_records(config, ["a.example.com", "b.example.com"])
    config_repo.save(config)

    _apply_overrides(db_session)
    app.dependency_overrides[get_record_config_repo] = lambda: RecordConfigRepository(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/bulk-set-cf", data={"enabled": "true"})
    app.dependency_overrides.clear()

    assert response.status_code == 200
    rcc = RecordConfigRepository(db_session)
    assert rcc.get("a.example.com").cf_enabled is True
    assert rcc.get("b.example.com").cf_enabled is True


def test_bulk_set_cf_disables_all_records(db_session):
    """POST /bulk-set-cf enabled=false must flip cf_enabled off on every record."""
    from dependencies import get_record_config_repo

    config_repo = ConfigRepository(db_session)
    config = config_repo.load()
    config_repo.set_records(config, ["a.example.com"])
    config_repo.save(config)

    _apply_overrides(db_session)
    app.dependency_overrides[get_record_config_repo] = lambda: RecordConfigRepository(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/bulk-set-cf", data={"enabled": "false"})
    app.dependency_overrides.clear()

    assert RecordConfigRepository(db_session).get("a.example.com").cf_enabled is False


def test_bulk_set_unifi_disables_all_and_local(db_session):
    """
    POST /bulk-set-unifi enabled=false must disable UniFi and clear the .local
    companion flag on every managed record.
    """
    from dependencies import get_record_config_repo

    config_repo = ConfigRepository(db_session)
    config = config_repo.load()
    config_repo.set_records(config, ["a.example.com"])
    config_repo.save(config)
    rcc = RecordConfigRepository(db_session)
    rcc.save(RecordConfig(record_name="a.example.com", unifi_enabled=True, unifi_local_enabled=True))

    _apply_overrides(db_session)
    app.dependency_overrides[get_record_config_repo] = lambda: RecordConfigRepository(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/bulk-set-unifi", data={"enabled": "false"})
    app.dependency_overrides.clear()

    cfg = RecordConfigRepository(db_session).get("a.example.com")
    assert cfg.unifi_enabled is False
    assert cfg.unifi_local_enabled is False


# ---------------------------------------------------------------------------
# POST /create-record — failure path
# ---------------------------------------------------------------------------


def test_create_record_failure_returns_error_fragment(db_session):
    """
    POST /create-record must return an inline error fragment (not a reload)
    and must NOT persist the record when the Cloudflare call fails.
    """
    from dependencies import get_dns_service
    from exceptions import DnsProviderError

    class _FailingDnsService:
        """Stand-in whose create_dns_record always fails."""

        async def create_dns_record(self, record_name, ip, zones):
            raise DnsProviderError("zone not configured")

    _apply_overrides(db_session)
    app.dependency_overrides[get_dns_service] = lambda: _FailingDnsService()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/create-record",
            data={"record_name": "home.example.com", "record_ip": "1.2.3.4"},
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "alert-error" in response.text
    # The failed create must not have added the record to the managed list
    config_repo = ConfigRepository(db_session)
    records = config_repo.get_records(config_repo.load())
    assert "home.example.com" not in records


# ---------------------------------------------------------------------------
# POST /check-record — per-record manual check
# ---------------------------------------------------------------------------


def test_check_record_returns_status_badge(db_session):
    """POST /check-record must return a status badge for the checked record."""
    from dependencies import get_dns_service, get_record_config_repo

    class _StubDnsService:
        async def check_record_now(self, record_name, cfg, zones):
            return "unchanged"

    _apply_overrides(db_session)
    app.dependency_overrides[get_dns_service] = lambda: _StubDnsService()
    app.dependency_overrides[get_record_config_repo] = lambda: RecordConfigRepository(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/check-record", data={"record_name": "home.example.com"})
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "Up to date" in response.text
