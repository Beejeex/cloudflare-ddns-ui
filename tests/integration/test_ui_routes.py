"""
tests/integration/test_ui_routes.py

Integration tests for routes/ui_routes.py (full-page GET renders).
Uses FastAPI's TestClient with dependency overrides backed by the in-memory
SQLite fixture; all outbound HTTP is intercepted by respx.
"""

from __future__ import annotations

import httpx
import respx
from fastapi.testclient import TestClient

from app import app
from cloudflare.dns_provider import DnsRecord
from dependencies import (
    get_config_service,
    get_dns_service,
    get_kubernetes_service,
    get_record_config_repo,
    get_stats_repo,
    get_unifi_client,
)
from repositories.config_repository import ConfigRepository
from repositories.record_config_repository import RecordConfigRepository
from repositories.stats_repository import StatsRepository
from services.config_service import ConfigService
from services.kubernetes_service import KubernetesService


def _seed_configured(db_session) -> ConfigRepository:
    """Seeds a minimal configured state: token, one zone, one managed record."""
    config_repo = ConfigRepository(db_session)
    config = config_repo.load()
    config.api_token = "test-token"
    config_repo.set_zones(config, {"example.com": "zone1"})
    config_repo.set_records(config, ["home.example.com"])
    config_repo.save(config)
    return config_repo


def _apply_overrides(db_session, config_repo: ConfigRepository) -> None:
    """Installs dependency overrides backed by the test DB session."""
    app.dependency_overrides[get_config_service] = lambda: ConfigService(config_repo)
    app.dependency_overrides[get_dns_service] = lambda: _StubDnsService()
    app.dependency_overrides[get_stats_repo] = lambda: StatsRepository(db_session)
    app.dependency_overrides[get_unifi_client] = lambda: _StubUnifiClient()
    app.dependency_overrides[get_record_config_repo] = lambda: RecordConfigRepository(db_session)
    app.dependency_overrides[get_kubernetes_service] = lambda: KubernetesService(enabled=False)


class _StubDnsService:
    """Minimal DnsService stand-in exposing only the two bulk fetches the dashboard uses."""

    def __init__(self) -> None:
        self._record = DnsRecord(
            id="rec1", name="home.example.com", content="1.2.3.4",
            type="A", ttl=1, proxied=False, zone_id="zone1",
        )

    async def fetch_zone_record_map(self, records, zones):
        return {r: self._record for r in records}

    async def list_zone_records(self, zones):
        return [self._record]


class _StubUnifiClient:
    """Disabled UniFi client — no policy calls are attempted."""

    def is_configured(self):
        return False


def test_dashboard_renders_when_configured(db_session):
    """GET / must render the dashboard page with the managed record visible."""
    config_repo = _seed_configured(db_session)
    # Opt the record into Cloudflare DDNS so its card shows the CF badge
    rc_repo = RecordConfigRepository(db_session)
    rc = rc_repo.get("home.example.com")
    rc.cf_enabled = True
    rc_repo.save(rc)
    _apply_overrides(db_session, config_repo)

    with respx.mock:
        respx.get("https://api.ipify.org").mock(return_value=httpx.Response(200, text="1.2.3.4"))
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "home.example.com" in response.text
    # Managed record with matching DNS IP renders as synced
    assert "CF synced" in response.text


def test_dashboard_renders_when_not_configured(db_session):
    """GET / must render a helpful banner when no token/zones are configured."""
    config_repo = ConfigRepository(db_session)
    _apply_overrides(db_session, config_repo)

    with respx.mock:
        respx.get("https://api.ipify.org").mock(return_value=httpx.Response(200, text="1.2.3.4"))
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "No API token or zones configured" in response.text


def test_settings_page_renders(db_session):
    """GET /settings must render with the log retention field present."""
    config_repo = _seed_configured(db_session)
    _apply_overrides(db_session, config_repo)

    with respx.mock:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/settings")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "log_retention_days" in response.text
    assert "Log Retention" in response.text
