# Migration Plan — Flask → FastAPI + SQLModel + HTMX

> **Versioning:** The current Flask app is tagged `v1.0.0`. The completed FastAPI rewrite will be tagged `v2.0.0`.

## Summary of Changes

| What | From | To |
|---|---|---|
| Web framework | Flask | FastAPI (async) |
| HTTP client | requests | httpx (async) |
| Templates | Jinja2 (redirect after POST) | Jinja2 + HTMX (partial fragments) |
| Background job | threading + time.sleep | APScheduler (AsyncIOScheduler) |
| Config storage | config/config.json (flat file) | SQLite table via SQLModel |
| Stats storage | logs/record_stats.json (flat file) | SQLite table via SQLModel |
| Log storage | logs/ddns.log (plain text) | SQLite table via SQLModel (optional, keep file as fallback) |
| Config file watching | none | watchdog observer |
| Dependency injection | none (direct imports) | FastAPI Depends() providers |
| Python version | 3.11 | 3.12 |
| Container base image | python:3.11-slim | python:3.12-slim |

---

## Phase 1 — Dependencies & Project Scaffold

**Goal:** Replace requirements.txt, update Dockerfile, and create the new folder structure.

### Tasks

- [ ] **1.1** Update `requirements.txt`:
  ```
  fastapi
  uvicorn[standard]
  jinja2
  httpx
  sqlmodel
  apscheduler
  watchdog
  tldextract
  aiofiles
  python-multipart    # required for FastAPI Form() parsing
  # --- testing ---
  pytest
  pytest-asyncio
  pytest-cov
  respx
  ```

- [ ] **1.2** Update `dockerfile`:
  - Change base image from `python:3.11-slim` to `python:3.12-slim`
  - Change start command from `python app.py` to `uvicorn app:app --host 0.0.0.0 --port 8080`
  - Add `RUN apt-get install -y curl` for the health check probe
  - Add `HEALTHCHECK` directive pointing at `GET /health`
  - Pre-create `/config/logs` directory in image so it works without a volume
  - Serve HTMX from `/static/htmx.min.js` — no CDN at runtime

- [ ] **1.3** Create `__init__.py` in every new package folder so Python treats them as importable packages:
  ```
  db/__init__.py
  services/__init__.py
  repositories/__init__.py
  cloudflare/__init__.py
  routes/__init__.py
  tests/__init__.py
  tests/unit/__init__.py
  tests/integration/__init__.py
  ```

- [ ] **1.4** Create empty folder structure:
  ```
  db/                    # SQLite engine, session factory, SQLModel table models
  services/              # Business logic services (ip, dns, config, stats, log)
  repositories/          # DB access layer (config, stats)
  cloudflare/            # DNSProvider protocol + CloudflareClient implementation
  routes/                # FastAPI routers (ui_routes, action_routes, api_routes)
  static/                # Static files — htmx.min.js served locally at runtime
  templates/             # Jinja2 full-page templates (base, dashboard, config, logs)
  templates/partials/    # HTMX partial fragments returned by POST handlers
  tests/                 # All tests — never touch real DB or real network
  tests/unit/            # Per-service and per-repository unit tests
  tests/integration/     # Route handler integration tests via TestClient
  todo/                  # Migration plan lives here
  ```

---

## Phase 2 — Database Layer

**Goal:** Replace JSON flat-files with SQLite via SQLModel. Nothing else changes logic yet.

### Tasks

- [ ] **2.1** Create `db/models.py` — define all SQLModel table models:
  - `AppConfig` table: `api_token`, `zones` (JSON string), `refresh`, `interval`, `ui_state` (JSON string)
  - `RecordStats` table: `record_name`, `last_checked`, `last_updated`, `updates`, `failures`
  - `LogEntry` table: `id`, `timestamp`, `level`, `message`, `is_api` (optional — may keep file-based logs)

- [ ] **2.2** Create `db/database.py`:
  - Create SQLite engine pointed at `/config/ddns.db` (matches Docker volume mount)
  - Provide `get_session()` async generator for FastAPI `Depends()`
  - Export `create_db_and_tables()` to be called once on startup

- [ ] **2.3** Create `repositories/config_repository.py`:
  - `load(session) -> AppConfig`
  - `save(session, config: AppConfig) -> None`
  - Reads and writes only the `AppConfig` table row
  - Does NOT contain business logic

- [ ] **2.4** Create `repositories/stats_repository.py`:
  - `get(session, record_name: str) -> RecordStats | None`
  - `upsert(session, stats: RecordStats) -> None`
  - `list_all(session) -> list[RecordStats]`
  - Does NOT contain business logic

---

## Phase 3 — Services Layer

**Goal:** Create all service classes with proper SOLID structure. Services are framework-agnostic (no FastAPI imports).

### Tasks

- [ ] **3.1** Create `services/ip_service.py`:
  - `IpService(http_client: httpx.AsyncClient)`
  - `async def get_public_ip() -> str`
  - Uses `https://api.ipify.org`
  - Accept injected `httpx.AsyncClient` so tests can mock network

- [ ] **3.2** Create `services/config_service.py`:
  - `ConfigService(repo: ConfigRepository)`
  - `async def load() -> AppConfig`
  - `async def save(config: AppConfig) -> None`
  - `async def get_ui_state() -> dict`
  - `async def set_ui_state(state: dict) -> None`
  - `async def add_managed_record(name: str) -> None`
  - `async def remove_managed_record(name: str) -> None`
  - `async def list_managed_records() -> list[str]`

- [ ] **3.3** Create `services/stats_service.py`:
  - `StatsService(repo: StatsRepository)`
  - `async def record_check(record_name: str) -> None`
  - `async def record_update(record_name: str) -> None`
  - `async def record_failure(record_name: str) -> None`
  - `async def get_all() -> list[RecordStats]`

- [ ] **3.4** Create `services/dns_service.py`:
  - `DnsService(provider: DNSProvider, ip_service: IpService, stats_service: StatsService)`
  - `async def check_and_update_all(records: list[str]) -> None`
  - `async def check_and_update_one(record_name: str) -> bool`
  - Does NOT import CloudflareClient directly — depends on DNSProvider abstraction

- [ ] **3.5** Create `services/log_service.py`:
  - `LogService()`
  - `async def read_recent(limit: int) -> list[LogEntry]`
  - `async def read_api_logs() -> dict`
  - `async def clear() -> None`
  - Parses existing `ddns.log` file; does NOT write logs

---

## Phase 4 — Exceptions & Cloudflare Client

**Goal:** Define all custom exceptions in one place, then build the abstracted Cloudflare client.

### Tasks

- [ ] **4.0** Create `exceptions.py` at the project root:
  - `IpFetchError(Exception)` — raised by `IpService` when public IP fetch fails
  - `DnsProviderError(Exception)` — raised by any `DNSProvider` implementation on API failure
  - `ConfigLoadError(Exception)` — raised by `ConfigRepository` when DB row is missing or corrupt
  - This is the **only** file that defines custom exceptions. All other modules import from here.

- [ ] **4.1** Create `cloudflare/dns_provider.py`:
  - Define `DNSProvider` as a `Protocol` (or ABC):
    ```python
    class DNSProvider(Protocol):
        async def get_record(self, zone_id: str, name: str) -> DnsRecord | None: ...
        async def update_record(self, zone_id: str, record: DnsRecord, new_ip: str) -> bool: ...
        async def create_record(self, zone_id: str, name: str, ip: str) -> DnsRecord: ...
        async def delete_record(self, zone_id: str, record_id: str) -> bool: ...
        async def list_records(self, zone_id: str) -> list[DnsRecord]: ...
    ```
  - Define `DnsRecord` dataclass: `id`, `name`, `content` (IP), `zone_id`, `proxied`, `ttl`

- [ ] **4.2** Create `cloudflare/cloudflare_client.py`:
  - `CloudflareClient(api_token: str, http_client: httpx.AsyncClient)`
  - Implements all `DNSProvider` methods
  - All HTTP calls go through the injected `httpx.AsyncClient`
  - Resolves `zone_id` from record name using `tldextract` internally
  - Raise `DnsProviderError` on API failures

---

## Phase 5 — Scheduler & Watcher

**Goal:** Replace the `updater.py` threading loop with APScheduler; add watchdog.

### Tasks

- [ ] **5.1** Create `scheduler.py`:
  - `create_scheduler(dns_service: DnsService, config_service: ConfigService) -> AsyncIOScheduler`
  - Registers one async job: `run_ddns_check(dns_service, config_service)`
  - Job interval is read from config; scheduler is started/stopped in `app.py` lifespan
  - The job function itself contains no business logic — only calls `dns_service`

- [ ] **5.2** Create `watcher.py`:
  - Sets up a `watchdog` `Observer` watching the `data/` directory
  - On config DB change detected externally, triggers a config reload signal
  - Does NOT contain DNS or config business logic
  - Started/stopped in `app.py` lifespan

---

## Phase 6 — FastAPI App & Dependency Injection

**Goal:** Build the new app entry point with proper lifespan management and `Depends()` wiring.

### Tasks

- [ ] **6.1** Create `dependencies.py`:
  - `get_db_session()` — yields SQLModel session
  - `get_http_client()` — yields a shared `httpx.AsyncClient`
  - `get_config_service(session, ...)` — builds and returns `ConfigService`
  - `get_stats_service(session, ...)` — builds and returns `StatsService`
  - `get_ip_service(client, ...)` — builds and returns `IpService`
  - `get_dns_provider(client, config_service, ...)` — builds and returns `CloudflareClient`
  - `get_dns_service(provider, ip_service, stats_service)` — builds and returns `DnsService`
  - `get_log_service()` — builds and returns `LogService`

- [ ] **6.2** Create `app.py` (FastAPI version):
  - Call `logging.basicConfig(...)` at module level before the `FastAPI()` instance is created
  - Use `@asynccontextmanager` lifespan to:
    1. Call `create_db_and_tables()`
    2. Create shared `httpx.AsyncClient`
    3. Start APScheduler
    4. Start watchdog observer
    5. On shutdown: stop scheduler, stop observer, close http client
  - Mount `static/` directory: `app.mount("/static", StaticFiles(directory="static"), name="static")`
  - Register all routers from `routes/`
  - Register custom exception handlers for `DnsProviderError`, `ConfigLoadError`
  - Expose `GET /health` returning `{"status": "ok"}`

---

## Phase 7 — Routes

**Goal:** Replace `routes.py` with HTMX-compatible FastAPI route modules.

### Tasks

- [ ] **7.1** Create `routes/ui_routes.py`:
  - `GET /` — renders full `index.html` page
  - All data fetched via services injected through `Depends()`
  - No business logic, no direct DB/file access

- [ ] **7.2** Create `routes/action_routes.py` — all POST handlers returning HTMX fragments:
  - `POST /update-config` → returns settings panel partial
  - `POST /add-to-managed` → returns records table partial
  - `POST /remove-from-managed` → returns records table partial
  - `POST /create-managed` → returns records table partial
  - `POST /delete-record` → returns records table partial
  - `POST /update` (manual trigger) → returns record row partial
  - `POST /update-ui-state` → returns `200 OK` (HTMX no-swap)
  - `POST /clear-logs` → returns logs panel partial
  - **None of these return `RedirectResponse`**

- [ ] **7.3** Create `routes/api_routes.py` (optional JSON API):
  - `GET /api/status` — returns current IP and record statuses as JSON
  - Useful for health checks and monitoring integrations

---

## Phase 8 — Templates

**Goal:** Update Jinja2 templates to use HTMX for partial swaps instead of full-page reloads.

### Tasks

- [ ] **8.1** Add HTMX CDN script to `templates/index.html` base layout.

- [ ] **8.2** Convert all `<form action="..." method="POST">` forms:
  - Replace `action` + `method` with `hx-post="/route"`
  - Add `hx-target="#target-element-id"` pointing at the section to update
  - Add `hx-swap="innerHTML"` (or `outerHTML` where appropriate)

- [ ] **8.3** Extract partial templates into `templates/partials/`:
  - `partials/records_table.html`
  - `partials/settings_panel.html`
  - `partials/logs_panel.html`
  - `partials/record_row.html`

- [ ] **8.4** Remove all `redirect(url_for(...))` patterns from templates (these now come from routes).

---

## Phase 9 — UI Overhaul

**Goal:** Replace the current single-file inline-CSS Flask UI with the design system used in MadTracked (`beejeex/madtracked`). Same tech stack, same visual language — dark nav + light card body, pure custom CSS, no framework.

**Reference:** `beejeex/madtracked` → `app/templates/`

### Design System (copy from MadTracked)

| Element | Value |
|---|---|
| Font | `system-ui, sans-serif` |
| Body background | `#f1f5f9` |
| Nav background | `#1e293b` (dark) |
| Nav link color | `#94a3b8` |
| Card background | `#ffffff`, border `#e2e8f0`, `border-radius: 0.5rem`, shadow |
| Primary button | bg `#0284c7`, hover `#0369a1` |
| Danger button | bg `#dc2626`, hover `#b91c1c` |
| Secondary button | bg `#e2e8f0`, color `#1e293b` |
| Log terminal | bg `#0f172a`, text `#a3e635` |
| Badge — up-to-date | `#16a34a` green |
| Badge — needs update | `#f59e0b` amber |
| Badge — error | `#dc2626` red |

### Tasks

- [ ] **10.1** Create `templates/base.html` with:
  - Dark nav bar with brand name + page links (Dashboard, Config, Logs)
  - Full CSS block matching MadTracked design system (cards, badges, table, forms, buttons)
  - HTMX CDN `<script>` tag
  - `{% block content %}{% endblock %}` body slot

- [ ] **10.2** Create `templates/dashboard.html` extending `base.html`:
  - Stat card grid: Current IP, total records, records up-to-date, records needing update, errors
  - Records table with status badges, last checked, last updated, update count
  - Manual "Update Now" button per record (HTMX post → row partial swap)

- [ ] **10.3** Create `templates/config.html` extending `base.html`:
  - Cloudflare API token field (password input)
  - Zones editor (JSON textarea or key/value row builder)
  - Background check interval + UI refresh interval inputs
  - Save button — HTMX post → settings panel partial swap (no full reload)

- [ ] **10.4** Create `templates/logs.html` extending `base.html`:
  - Dark terminal-style `<pre>` log viewer
  - HTMX auto-refresh every 5s (`hx-trigger="load, every 5s"`)
  - Clear logs button (HTMX post → empties log output immediately)

- [ ] **10.5** Create `templates/partials/` fragments for HTMX swaps:
  - `partials/records_table.html` — full records table body
  - `partials/record_row.html` — single record `<tr>` for inline updates
  - `partials/settings_panel.html` — settings form section
  - `partials/logs_panel.html` — log `<pre>` content only

- [ ] **9.6** Before final Docker build: download HTMX to `static/htmx.min.js` and serve locally so the container has no CDN dependency at runtime.

---

## Phase 10 — Unit & Integration Tests

**Goal:** Every service, repository, and route handler has tests. No test makes a real network call or touches the real database.

### Tasks

- [ ] **10.1** Create `pytest.ini` at the project root:
  ```ini
  [pytest]
  asyncio_mode = auto
  testpaths = tests
  ```

- [ ] **10.2** Create `tests/conftest.py` with shared fixtures:
  - `db_session` — in-memory SQLite session (fresh tables per test, dropped after)
  - `mock_http` — `respx.mock` router; intercepts all `httpx` calls so no real traffic leaves the container

- [ ] **10.3** Unit tests for services (`tests/unit/`):
  - `test_ip_service.py` — happy path (returns IP string), failure path (raises `IpFetchError`)
  - `test_dns_service.py` — no update needed, update triggered, update fails (raises `DnsProviderError`)
  - `test_config_service.py` — load, save, add/remove managed record
  - `test_stats_service.py` — record_check, record_update, record_failure increments
  - `test_log_service.py` — read_recent returns correct slice, clear wipes entries

- [ ] **10.4** Unit tests for repositories (`tests/unit/`):
  - `test_config_repository.py` — load returns defaults on empty DB, save persists row
  - `test_stats_repository.py` — get returns None for unknown record, upsert creates and updates

- [ ] **10.5** Unit tests for Cloudflare client (`tests/unit/test_cloudflare_client.py`):
  - `get_record` returns `DnsRecord` on 200, returns `None` on empty result
  - `update_record` sends correct PUT payload, raises `DnsProviderError` on non-2xx
  - `create_record` sends correct POST payload
  - `delete_record` sends correct DELETE, raises on failure
  - All tests use `respx.mock` — no real calls to `api.cloudflare.com`

- [ ] **10.6** Integration tests for routes (`tests/integration/`):
  - `test_ui_routes.py` — `GET /` returns 200 and contains expected HTML elements
  - `test_action_routes.py` — all POST handlers return 200 HTML fragment (not 302 redirect)
  - `test_api_routes.py` — `GET /api/status` returns valid JSON
  - All tests use `TestClient` with `app.dependency_overrides` to inject test doubles

- [ ] **10.7** Add coverage check to `pytest.ini`:
  ```ini
  addopts = --cov=. --cov-report=term-missing --cov-fail-under=80
  ```

---

## Phase 11 — Cleanup

**Goal:** Remove all old files. Only do this phase once Phase 10 tests are passing with no failures.

### Tasks

- [ ] **11.1** All Phase 10 tests must be green before touching any file in this phase.
- [ ] **11.2** Delete `app.py` (old Flask version — replaced by new `app.py`)
- [ ] **11.3** Delete `routes.py` (replaced by `routes/`)
- [ ] **11.4** Delete `updater.py` (replaced by `scheduler.py`)
- [ ] **11.5** Delete `cloudflare_api.py` (replaced by `cloudflare/cloudflare_client.py`)
- [ ] **11.6** Delete `config.py` (replaced by `services/config_service.py` + `repositories/config_repository.py`)
- [ ] **11.7** Verify `logger.py` is write-only; confirm reader/cleanup parts are covered by `log_service.py` and `log_cleanup.py`, then remove those parts from `logger.py`.
- [ ] **11.8** Run full test suite again after deletions to confirm nothing broke.

---

## Phase 12 — Smoke Test & Release

**Goal:** Manually verify the running container end-to-end before tagging a release.

### Tasks

- [ ] **12.1** Build and run the Docker image locally:
  ```bash
  docker build -t ddns-dashboard:dev .
  docker run -d -p 8080:8080 -v $(pwd)/testconfig:/config --name ddns-test ddns-dashboard:dev
  ```
- [ ] **12.2** Verify `GET /health` returns `{"status": "ok"}`.
- [ ] **12.3** Open the dashboard — confirm stat cards, records table, and logs panel all render correctly.
- [ ] **12.4** Add a managed record via the UI; confirm it appears in the table without a full page reload.
- [ ] **12.5** Trigger a manual update; confirm the record row updates in place via HTMX.
- [ ] **12.6** Restart the container; confirm config and stats survive (volume mount working).
- [ ] **12.7** Tag the release:
  ```bash
  git tag v2.0.0
  git push origin v2.0.0
  ```

---

## Notes & Decisions

- **Config migration**: First run after migration should seed the `AppConfig` table from the existing `config/config.json` if it exists. Add a one-time migration helper in `db/database.py`.
- **Log file vs DB**: Keep the existing file-based log (`logs/ddns.log`) as the write target for simplicity. `LogService` reads from it. Only stats and config move to SQLite.
- **watchdog scope**: Watch the `config/` directory for any external edits to `config.json` during the transition, then watch `data/` once fully migrated to SQLite.
- **Docker single container**: No compose file needed for normal use. The SQLite DB and logs all live under the single `/config` volume mount.
- **HTMX CDN vs local**: Use CDN during development. Before final Docker build, download to `static/htmx.min.js` and reference locally — the container must work with no internet access.
- **Test isolation**: The `db_session` fixture uses `StaticPool` so the same in-memory engine is reused within a test without threading issues.

---

## Future Ideas (out of scope for this migration)

These are not part of the current refactor. Document here so the architecture can support them without rework.

### Internal DNS Mode — Kubernetes + UniFi

**Concept:** A second operating mode alongside the existing public Cloudflare DDNS mode. Instead of updating external DNS records when the public IP changes, this mode manages **internal DNS records** for services running on the local network.

**Two planned providers:**

| Provider | What it does |
|---|---|
| **Kubernetes Ingress reader** | Watches `Ingress` resources in a cluster; extracts hostnames and target IPs; writes/updates internal A-records via the configured DNS backend |
| **UniFi Network API** | Uses the UniFi controller REST API to create and update local DNS records (static host mappings) for internal hostnames |

**How the architecture supports this already:**
- `DNSProvider` abstraction in `cloudflare/dns_provider.py` is the only interface `dns_service.py` depends on.
- Adding `kubernetes_client.py` or `unifi_client.py` inside the `cloudflare/` folder (or a new `providers/` folder) requires zero changes to any existing service, route, or scheduler.
- A new config field `mode: "cloudflare" | "kubernetes" | "unifi"` in `AppConfig` would determine which `DNSProvider` implementation is injected via `dependencies.py`.

**Prerequisites before starting:**
- Complete Phase 1–11 (core migration) first.
- Define a stable `DnsRecord` dataclass and `DNSProvider` contract before adding new implementations.
- Add `kubernetes` and `unifi` as optional dependency groups in `requirements.txt` so they don't bloat the default image.
