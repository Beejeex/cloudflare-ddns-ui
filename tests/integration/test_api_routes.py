"""
tests/integration/test_api_routes.py

Integration tests for routes/api_routes.py.
Uses FastAPI's TestClient as a context manager so the lifespan starts and stops
cleanly for each test. Depends() providers are overridden with test doubles
backed by the in-memory SQLite fixture.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import app
from dependencies import (
    get_config_service,
    get_history_service,
    get_log_service,
    get_record_config_repo,
    get_stats_service,
)
from repositories.config_repository import ConfigRepository
from repositories.history_repository import HistoryRepository
from repositories.record_config_repository import RecordConfigRepository
from repositories.stats_repository import StatsRepository
from services.config_service import ConfigService
from services.history_service import HistoryService
from services.log_service import LogService
from services.stats_service import StatsService


def test_health_endpoint_returns_ok():
    """GET /health must return status ok with the app version."""
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_metrics_endpoint_returns_prometheus_format():
    """GET /metrics must return Prometheus exposition with the metric names."""
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "ddns_checks_total" in response.text
    assert "ddns_cycle_duration_seconds" in response.text


def test_record_history_returns_fragment(db_session):
    """GET /api/records/{name}/history must render the history timeline fragment."""
    service = HistoryService(HistoryRepository(db_session))
    service.record_ip_change("home.example.com", "1.2.3.4", source="scheduler")
    service.record_ip_change("home.example.com", "9.9.9.9", source="manual")

    app.dependency_overrides[get_history_service] = lambda: HistoryService(HistoryRepository(db_session))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/records/home.example.com/history")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "9.9.9.9" in response.text
    assert "manual" in response.text
    assert "1.2.3.4" in response.text


def test_health_ready_endpoint_reports_components():
    """GET /health/ready must report database and scheduler readiness."""
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["database"] is True
    assert body["scheduler"] is True
    assert body["status"] == "ok"
    assert body["version"]


def test_get_recent_logs_returns_html(db_session):
    """GET /api/logs/recent must return an HTML fragment for HTMX polling."""
    config_repo = ConfigRepository(db_session)
    stats_repo = StatsRepository(db_session)
    app.dependency_overrides[get_config_service] = lambda: ConfigService(config_repo)
    app.dependency_overrides[get_stats_service] = lambda: StatsService(stats_repo)
    app.dependency_overrides[get_log_service] = lambda: LogService(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/logs/recent")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_get_recent_logs_filters_by_level(db_session):
    """GET /api/logs/recent?level=ERROR must return only ERROR entries."""
    service = LogService(db_session)
    service.log("an info entry", level="INFO")
    service.log("a warning entry", level="WARNING")
    service.log("an error entry", level="ERROR")

    app.dependency_overrides[get_log_service] = lambda: LogService(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/logs/recent?level=ERROR")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "an error entry" in response.text
    assert "an info entry" not in response.text
    assert "a warning entry" not in response.text


def test_export_logs_csv(db_session):
    """GET /api/logs/export must return a CSV download of the log entries."""
    service = LogService(db_session)
    service.log("export me", level="INFO")
    service.log("an error", level="ERROR")

    app.dependency_overrides[get_log_service] = lambda: LogService(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/logs/export")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="ddns-logs-' in response.headers["content-disposition"]
    assert "timestamp,level,message" in response.text
    assert "export me" in response.text
    assert "an error" in response.text


def test_health_json_returns_ok():
    """GET /api/health/json must return {"status": "ok"}."""
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/health/json")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_unifi_sites_accepts_form_body():
    """
    POST /api/unifi/sites must take the API key from the form body (not a
    query parameter, which proxies may log) and return a site picker fragment.
    """
    import httpx
    import respx

    from dependencies import get_unifi_http_client

    async def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient()

    app.dependency_overrides[get_unifi_http_client] = _client
    with respx.mock:
        respx.get(
            "https://unifi.test/proxy/network/integration/v1/sites"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "site-1", "name": "Default"}]},
            )
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/unifi/sites",
                data={"unifi_host": "unifi.test", "unifi_api_key": "key-1234"},
            )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "Default" in response.text


def test_verify_token_returns_zone_fragment():
    """
    POST /api/verify-token must verify the token (form body, not query) and
    return click-to-add zone buttons.
    """
    import httpx
    import respx

    with respx.mock:
        respx.get("https://api.cloudflare.com/client/v4/user/tokens/verify").mock(
            return_value=httpx.Response(
                200,
                json={"success": True, "result": {"id": "tok", "status": "active"}, "errors": []},
            )
        )
        respx.get("https://api.cloudflare.com/client/v4/zones").mock(
            return_value=httpx.Response(
                200,
                json={"success": True, "result": [{"name": "example.com", "id": "zone1"}], "errors": []},
            )
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/api/verify-token", data={"api_token": "test-token"})

    assert response.status_code == 200
    assert "Token valid" in response.text
    assert "example.com" in response.text


def test_verify_token_reports_invalid_token():
    """POST /api/verify-token must surface an inactive token without a crash."""
    import httpx
    import respx

    with respx.mock:
        respx.get("https://api.cloudflare.com/client/v4/user/tokens/verify").mock(
            return_value=httpx.Response(
                200,
                json={"success": True, "result": {"id": "tok", "status": "disabled"}, "errors": []},
            )
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/api/verify-token", data={"api_token": "bad-token"})

    assert response.status_code == 200
    assert "invalid or inactive" in response.text


# ---------------------------------------------------------------------------
# Config export / import
# ---------------------------------------------------------------------------


def test_export_import_roundtrip(db_session):
    """
    GET /api/export must produce a payload that POST /api/import restores.
    """
    config_repo = ConfigRepository(db_session)
    config = config_repo.load()
    config.api_token = "tok123"
    config_repo.set_zones(config, {"example.com": "zone1"})
    config_repo.set_records(config, ["home.example.com"])
    config_repo.save(config)

    rc_repo = RecordConfigRepository(db_session)
    rc = rc_repo.get("home.example.com")
    rc.cf_enabled = True
    rc.ip_mode = "static"
    rc.static_ip = "5.5.5.5"
    rc_repo.save(rc)

    app.dependency_overrides[get_config_service] = lambda: ConfigService(config_repo)
    app.dependency_overrides[get_record_config_repo] = lambda: rc_repo
    app.dependency_overrides[get_log_service] = lambda: LogService(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        exported = client.get("/api/export")
        assert exported.status_code == 200
        payload = exported.json()

        assert payload["config"]["api_token"] == "tok123"
        assert payload["config"]["zones"] == {"example.com": "zone1"}
        assert payload["config"]["records"] == ["home.example.com"]
        assert payload["record_configs"][0]["static_ip"] == "5.5.5.5"

        imported = client.post("/api/import", json=payload)
        assert imported.status_code == 200
        assert imported.json()["records"] == 1
        assert imported.json()["zones"] == 1
    app.dependency_overrides.clear()

    # The DB must match the exported state after the import round trip
    config = config_repo.load()
    assert config.api_token == "tok123"
    assert config_repo.get_records(config) == ["home.example.com"]
    rc_after = rc_repo.get("home.example.com")
    assert rc_after.static_ip == "5.5.5.5"


# ---------------------------------------------------------------------------
# SSE /api/events
# ---------------------------------------------------------------------------


def test_sse_events_route_is_registered():
    """GET /api/events must be exposed in the app's OpenAPI schema.

    NOTE: app.routes no longer flattens included routers (Starlette 1.6+
    wraps them in _IncludedRouter objects), so route existence is asserted
    via the generated OpenAPI paths instead of the raw route list.

    Full end-to-end SSE streaming is tested implicitly by the BroadcastService
    unit tests; live HTTP streaming tests require a real server and are out of
    scope for the synchronous TestClient.
    """
    paths = app.openapi()["paths"]
    assert "/api/events" in paths


def test_sse_broadcaster_publishes_event_to_subscriber():
    """Events published after SSE subscribe must be retrievable from the queue.

    Verifies the integration point between the broadcaster (injected into the
    SSE endpoint via Depends) and the per-client subscriber queue.
    """
    import asyncio
    from services.broadcast_service import BroadcastService

    svc = BroadcastService()
    q = svc.subscribe()
    svc.publish("ip_updated", "1.2.3.4")
    msg = q.get_nowait()

    assert msg["event"] == "ip_updated"
    assert msg["data"] == "1.2.3.4"
    assert len(svc._queues) == 1  # Queue still registered until unsubscribe

    svc.unsubscribe(q)
    assert len(svc._queues) == 0


# ---------------------------------------------------------------------------
# GET /api/records — static-IP up-to-date logic
# ---------------------------------------------------------------------------


def test_records_static_mode_up_to_date_uses_static_ip(db_session):
    """
    GET /api/records must judge a static-IP record against cfg.static_ip,
    not against the detected public IP (which differs here).
    """
    import httpx
    import respx

    from cloudflare.dns_provider import DnsRecord
    from dependencies import (
        get_dns_service,
        get_record_config_repo,
        get_stats_repo,
        get_unifi_client,
    )
    from repositories.record_config_repository import RecordConfigRepository

    # Seed one managed record configured for static IP mode
    config_repo = ConfigRepository(db_session)
    config = config_repo.load()
    config.api_token = "test-token"
    config_repo.set_zones(config, {"example.com": "zone1"})
    config_repo.set_records(config, ["home.example.com"])
    config_repo.save(config)

    rc_repo = RecordConfigRepository(db_session)
    rc = rc_repo.get("home.example.com")
    rc.cf_enabled = True
    rc.ip_mode = "static"
    rc.static_ip = "5.5.5.5"
    rc_repo.save(rc)

    class _StubDnsService:
        """Minimal stand-in exposing only fetch_zone_record_map()."""

        async def fetch_zone_record_map(self, records, zones):
            return {
                "home.example.com": DnsRecord(
                    id="rec1", name="home.example.com", content="5.5.5.5",
                    type="A", ttl=1, proxied=False, zone_id="zone1",
                )
            }

    class _StubUnifiClient:
        """UniFi client stub — disabled so no policy calls are attempted."""

        def is_configured(self):
            return False

    app.dependency_overrides[get_config_service] = lambda: ConfigService(config_repo)
    app.dependency_overrides[get_dns_service] = lambda: _StubDnsService()
    app.dependency_overrides[get_stats_repo] = lambda: StatsRepository(db_session)
    app.dependency_overrides[get_unifi_client] = lambda: _StubUnifiClient()
    app.dependency_overrides[get_record_config_repo] = lambda: rc_repo

    # The public IP differs from the static IP — up-to-date must still hold.
    with respx.mock:
        respx.get("https://api.ipify.org").mock(
            return_value=httpx.Response(200, text="1.2.3.4")
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/records")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "home.example.com" in response.text
    assert "CF synced" in response.text


def test_records_dynamic_mode_uses_public_ip(db_session):
    """
    GET /api/records must judge a dynamic-mode record against the detected
    public IP (regression guard for the static-mode fix above).
    """
    import httpx
    import respx

    from cloudflare.dns_provider import DnsRecord
    from dependencies import (
        get_dns_service,
        get_record_config_repo,
        get_stats_repo,
        get_unifi_client,
    )
    from repositories.record_config_repository import RecordConfigRepository

    config_repo = ConfigRepository(db_session)
    config = config_repo.load()
    config.api_token = "test-token"
    config_repo.set_zones(config, {"example.com": "zone1"})
    config_repo.set_records(config, ["home.example.com"])
    config_repo.save(config)

    rc_repo = RecordConfigRepository(db_session)
    rc = rc_repo.get("home.example.com")
    rc.cf_enabled = True
    rc.ip_mode = "dynamic"
    rc_repo.save(rc)

    class _StubDnsService:
        async def fetch_zone_record_map(self, records, zones):
            return {
                "home.example.com": DnsRecord(
                    id="rec1", name="home.example.com", content="1.2.3.4",
                    type="A", ttl=1, proxied=False, zone_id="zone1",
                )
            }

    class _StubUnifiClient:
        def is_configured(self):
            return False

    app.dependency_overrides[get_config_service] = lambda: ConfigService(config_repo)
    app.dependency_overrides[get_dns_service] = lambda: _StubDnsService()
    app.dependency_overrides[get_stats_repo] = lambda: StatsRepository(db_session)
    app.dependency_overrides[get_unifi_client] = lambda: _StubUnifiClient()
    app.dependency_overrides[get_record_config_repo] = lambda: rc_repo

    with respx.mock:
        respx.get("https://api.ipify.org").mock(
            return_value=httpx.Response(200, text="1.2.3.4")
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/records")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "CF synced" in response.text
