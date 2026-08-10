# TODO — Cloudflare DNS Dashboard

> Local-only development. **No** GitHub Actions, no automated builds, no CI/CD.
> All improvements are tracked here and implemented locally.
> Current version: **v2.1.5**

Check off items with `[x]` as they are completed.

---

## 🔴 P1 — Bugs & correctness

- [x] **#1 Log retention setting missing.** ~~README documents "Log Retention (default 30 days)" but there is no such setting — `scheduler.py` hardcodes `run_cleanup(session, days_to_keep=7)`.~~ **DONE** — added `log_retention_days` (default 7) to `AppConfig` + migration, Settings UI field, and `scheduler.py` now reads it from config. README updated to match.
      - Files: `scheduler.py`, `db/models.py`, `routes/ui_routes.py`, `templates/settings.html`, `README.md`
- [x] **#2 Cloudflare `list_records()` has no pagination.** ~~Calls `GET /dns_records?type=A` without `per_page` (CF default = 20). Zones with >20 A-records silently miss records in the discovery panel and `fetch_zone_record_map()`.~~ **DONE** — `list_records()` now pages with `per_page=100` following `result_info.total_pages` (dedupes by id).
      - Files: `cloudflare/cloudflare_client.py`
- [x] **#2b UniFi `list_records()` only fetches first 200** ~~(`offset=0, limit=200`), no pagination loop.~~ **DONE** — now follows `totalCount` with a defensive page cap.
      - Files: `cloudflare/unifi_client.py`
- [x] **#3 "Up to date" logic wrong for static-IP records.** ~~`is_up_to_date = (dns_ip == current_ip)` compares against the dynamic public IP even when `ip_mode="static"`. Should compare against `cfg.static_ip`.~~ **DONE** — routes now compare against `cfg.static_ip` in static mode.
      - Files: `routes/ui_routes.py`, `routes/api_routes.py`
- [x] **#6 Race condition on managed-records list.** ~~Records are a JSON array in the single `AppConfig.records_json` row — read-modify-write can lose entries under concurrent tabs.~~ **DONE** — a module-level `_records_lock` in `config_service` serialises the read-modify-write in `add_managed_record`/`remove_managed_record` (the critical section is await-free, so it is safe in the async single-process model).
      - Files: `services/config_service.py`, `repositories/config_repository.py`
- [x] **#7 `_to_local_policy_name` duplicated in 3 files.** ~~`scheduler.py`, `routes/ui_routes.py`, `routes/api_routes.py` — extract to one shared module.~~ **DONE** — extracted to `utils.py` (`to_local_policy_name`), all three files import it.
- [x] **#8 SSE broadcast re-fetches public IP upstream every cycle.** ~~`IpService` used in `_ddns_check_job` has no `app_state` so caching is disabled.~~ **DONE** — `app_state` is now threaded through `create_scheduler`/`run_ddns_check_now` into the job, so the cycle and broadcast share the `ip_cache`.
      - Files: `scheduler.py`
- [x] **#9 `/api/next-check-in` swallows DB errors (`except Exception: pass`)** ~~and builds repos inline instead of via `Depends()`.~~ **DONE** — now uses `Depends(get_config_service)`; narrows exception handling.
      - Files: `routes/api_routes.py`
- [x] **#10 `htmx.min.js` not vendored.** ~~`static/` only has `alpinejs.min.js` + `htmx-sse.js`; htmx is downloaded at Docker build. Local (non-Docker) dev 404s on `/static/htmx.min.js`.~~ **DONE** — `static/htmx.min.js` (v2.0.4) vendored; Docker build no longer downloads it. (Same fix as #51.)
- [x] **#11 Mixed/naive datetimes.** ~~`LogEntry` uses `datetime.utcnow()` (deprecated in 3.12); `LogService` strips tzinfo; stats timestamps naive.~~ **DONE** — added `utils.utcnow_naive()`; model, `LogService`, and `StatsRepository` all use it consistently (naive UTC everywhere).
      - Files: `db/models.py`, `services/log_service.py`
- [x] **#12 No retry on transient HTTP failures** ~~for IP provider / Cloudflare — one network blip fails the whole cycle.~~ **DONE** — retry-with-backoff (3 attempts, 4xx terminal) added to `IpService`, `CloudflareClient`, and `UnifiClient`, with unit tests.
      - Files: `services/ip_service.py`, `cloudflare/cloudflare_client.py`

---

## 🔐 P2 — Security (internal app — auth intentionally OUT OF SCOPE)

> **Decision (2026-08-08):** This is an internal, trusted-network app only. Authentication, CSRF, and rate limiting are **deliberately not planned**. Do not re-add them. Only defense-in-depth / hygiene items below.

- [x] **#5 Secrets exposed in plaintext.** ~~Full `api_token` rendered into `settings.html` value attributes; `/api/unifi/sites` takes the UniFi API key as a query param (logged by proxies).~~ **DONE** — settings page now renders masked placeholders (`mask_secret` in `utils.py`: bullets + last 4 chars) for the CF token and UniFi key; saving an unchanged masked placeholder keeps the stored secret instead of overwriting it; `/api/unifi/sites` is now POST with the key in the form body (never a query param). Verified live in the browser.
      - Files: `utils.py`, `templates/settings.html`, `routes/ui_routes.py`, `routes/action_routes.py`, `routes/api_routes.py`
- [x] **#14 UniFi CA options.** ~~`verify=False` is required for self-signed, but add optional CA bundle / pinned cert instead of disabling verification entirely.~~ **DONE** — `UNIFI_CA_BUNDLE` env var (PEM bundle path) switches the UniFi client to certificate verification when set; otherwise it keeps `verify=False` for self-signed certs.
      - Files: `app.py`
- [x] **#16 Secrets at rest in plaintext** ~~in SQLite. Document `/config` permissions~~; consider masking/encryption. **DONE (documentation)** — README now warns to restrict `/config` (chmod 700, never expose publicly) since the DB holds the API secrets in plaintext.

---

## 🏗 P2 — Architecture & code quality

- [x] **#17 `_ddns_check_job` is a god-function.** ~~Does CF cycle + UniFi pass + log cleanup + template rendering for SSE inline.~~ **DONE (UniFi)** — the UniFi sync pass is extracted into `services/unifi_service.py` (`UniFiService.sync_policies`), which encapsulates the one-call policy listing, per-record main + `.local` reconciliation, last_checked stamping, and the summary log. The job now just wires up the client and delegates. (The SSE render helper remains in the job but is small.)
      - Files: `scheduler.py`
- [x] **#18 Record-row dict building duplicated ~4×.** ~~`_build_record_rows` (`action_routes.py`), scheduler SSE render, `_render_records_for_sse`, `get_records` (`api_routes.py`).~~ **DONE** — extracted `presenters.build_record_row()`; all four call sites (action rows, scheduler SSE render, `/api/records`, dashboard) now use it. (`_render_records_for_sse` was already removed as dead code in #20.)
- [x] **#19 DI is inconsistent.** ~~Routes construct `IpService(request.app.state.http_client)` and repos inline instead of `Depends()` providers.~~ **DONE** — `current_ip`, `get_records`, and the dashboard now inject `IpService` via `Depends(get_ip_service)`; no route constructs services/repos inline anymore.
      - Files: `routes/ui_routes.py`, `routes/api_routes.py`
- [x] **#20 Dead code to remove.** ~~`_render_records_for_sse` (never called), `ui_state` config surface (`get_ui_state`/`set_ui_state` — unused), `kubeconfig_path` DB column (not in model, never read).~~ **DONE** — `_render_records_for_sse` removed; `ui_state` surface removed from service/repo/model; `kubeconfig_path` migration dropped.
      - Files: `routes/api_routes.py`, `services/config_service.py`, `repositories/config_repository.py`, `db/database.py`
- [x] **#21 Mid-file imports with `# noqa: E402`.** ~~`from shared_templates import templates` inside route files — move to top.~~ **DONE** — imports moved to top of all three route files.
      - Files: `routes/action_routes.py`, `routes/api_routes.py`, `routes/ui_routes.py`
- [x] **#22 Version string in two places.** ~~`app.py` `"2.1.5"` and `shared_templates.py` `"v2.1.5"` — single constant would remove mismatch risk.~~ **DONE** — `app.py` now derives its version from `APP_VERSION` in `shared_templates.py`.
- [x] **#23 Broad `except Exception` blocks.** ~~SSE generator, scheduler broadcast — narrow to specific exceptions where possible.~~ **DONE** — the `ip_updated` broadcast now catches `IpFetchError` only; the `records_updated` block keeps a broad catch (DB + template rendering have heterogeneous failure modes, noted in a comment).

---

## ⚡ P2 — Performance

- [x] **#24 Dashboard page load is sequential** ~~— IP, UniFi, CF zones, K8s all awaited one-by-one.~~ **DONE** — independent network lookups now run under `asyncio.gather()` (each with its own error handling), so page load scales with the slowest provider.
      - Files: `routes/ui_routes.py`
- [x] **#25 No cache of CF/UniFi listings** ~~between scheduler and UI — every page load / SSE reconnect re-fetches all zones and policies.~~ **DONE** — `CloudflareClient` and `UnifiClient` accept an optional shared TTL cache (30 s) populated from `app.state.listing_cache`; `list_records()`/`list_zones()`/`list_sites()` serve fresh entries without a network call, and every mutation (`create`/`update`/`delete`) invalidates the affected zone/site listing. Wired into `get_dns_provider`, `get_unifi_client`, the scheduler job, and the verify-token / unifi-sites routes. Cache-hit + invalidation unit tests.
      - Files: `utils.py`, `cloudflare/cloudflare_client.py`, `cloudflare/unifi_client.py`, `app.py`, `dependencies.py`, `scheduler.py`
- [x] **#26 No index on `LogEntry.timestamp`** ~~— `ORDER BY timestamp DESC` slows down as the log table grows.~~ **DONE** — `index=True` on the model plus an idempotent `CREATE INDEX IF NOT EXISTS` migration for existing DBs.
      - Files: `db/database.py` (migration) or `db/models.py`
- [x] **#27 Check cycle does many small commits** ~~(per record) — batch stats/log writes.~~ **DONE** — `DnsService.run_check_cycle(batch_commits=True)` (default) defers every per-record stats/log write and flushes with a single `session.commit()` at cycle end. `LogService.log(commit=…)` + `flush()`, `StatsRepository.save/get_or_create(commit=…)`, and `StatsService` passthrough support it; single-record paths (`check_record_now`) still commit immediately. Unit tests verify deferred commits + single flush.
      - Files: `services/dns_service.py`, `services/log_service.py`, `services/stats_service.py`, `repositories/stats_repository.py`

---

## 🧪 P2 — Testing

- [x] **#29 No tests for the scheduler** ~~(`_ddns_check_job`, `run_ddns_check_now`, `reschedule`), watcher, or `log_cleanup`.~~ **DONE** — added `test_scheduler.py` (job registration + reschedule), `test_watcher.py` (observer lifecycle + handler), `test_log_cleanup.py` (due/not-due scheduling + pruning).
      - Files: `tests/unit/`
- [ ] **#30 Coverage gaps.** Now covered: pagination, static-mode "up to date" logic, retry behaviour, dashboard smoke test, `/create-record` failure path, shared record-row presenter, log-level filtering, CSV export, IP-provider fallback chain, secret masking, UniFi sites POST form body, batch commits, listing cache (hit + invalidation), bulk CF/UniFi actions, IP history (repo/service/route/instrumentation), Prometheus metrics, DB upgrade path (`init_db` new-table creation + `_run_migrations` ALTER TABLE). A live `/api/events` stream test was attempted but is impractical with the sync TestClient (the SSE generator never completes) — SSE is covered by the route-registration test plus the `BroadcastService` unit tests.
- [x] **#31 No coverage threshold enforced** ~~despite `pytest-cov` being installed.~~ **DONE** — `pytest.ini` now enforces `--cov-fail-under=75` on every run (currently 77.6%). Targeted single-file runs need `--cov-fail-under=0` appended since they only cover a slice.
      - Files: `pytest.ini`
- [x] **#32 `requirements.txt` unpinned** ~~(bare `fastapi`, `httpx`, …)~~ — **DONE** — pinned to the exact versions verified in the container.
      - Files: `requirements.txt`

---

## 🎨 P2 — UI/UX

- [x] **#33 Empty states / onboarding wizard** ~~— first-run setup flow (enter token → verify → pick zones → add first record).~~ **DONE** — when no API token or zones are configured, the dashboard shows a "🚀 Get started" onboarding card with a 3-step walkthrough (add token → pick zones → add first record) and a "Go to Settings" button, driven by a new `first_run` template flag. Verified in a fresh container via Playwright.
      - Files: `templates/dashboard.html`, `routes/ui_routes.py`
- [x] **#34 Logs page has no filtering/pagination** ~~— only latest 100, no filter by level/record, no "load more". `LogService.get_by_level` exists but isn't exposed.~~ **DONE (level filter)** — `/api/logs/recent?level=…` and the logs page now support level filtering, with All/Info/Warnings/Errors buttons in the UI; SSE-driven refreshes keep the active filter. (Pagination / per-record filter still open.)
      - Files: `templates/logs.html`, `routes/api_routes.py`
- [x] **#35 No per-record "check now / force update"** ~~— only the global `/trigger-sync`.~~ **DONE** — added `DnsService.check_record_now()` (static/dynamic aware, same stats+log path as the scheduler), a `POST /check-record` route returning a status badge, and a "Check now" item in each managed card's ⋯ menu with a per-record status span. Verified end-to-end in the browser.
      - Files: `services/dns_service.py`, `routes/action_routes.py`, `templates/dashboard.html`
- [x] **#36 No bulk actions** ~~(enable/disable CF or UniFi for all records at once).~~ **DONE** — four buttons in the dashboard toolbar (`CF on/off`, `UniFi on/off`) post to new `/bulk-set-cf` and `/bulk-set-unifi` routes; each applies the flag to every managed record in a single DB transaction (UniFi off also clears `.local` companions), logs the action, broadcasts `records_updated`, and reloads the page. `RecordConfigRepository.set_flag_all`/`set_cf_enabled_all`/`set_unifi_enabled_all` added with unit + integration tests. Verified live in the browser (confirm modal, reload, card state, log entry).
      - Files: `repositories/record_config_repository.py`, `routes/action_routes.py`, `templates/dashboard.html`
- [x] **#37 Accessibility** — ~~toggles lack `aria-label`s, no visible focus states, small targets.~~ **DONE** — managed/unmanaged toggles are `role="switch"` with `aria-checked` + `aria-label`; menu buttons get `aria-label` + `aria-haspopup="menu"`; a global `:focus-visible` outline was added; toggle hit targets were enlarged. Verified in a live browser (Playwright MCP).
      - Files: `templates/base.html`, `templates/dashboard.html`
- [x] **#38 Mobile responsiveness** ~~— navbar `min-width: 220px` both sides, no mobile menu, grid likely overflows.~~ **DONE** — a `@media (max-width: 768px)` pass stacks the navbar (wraps into rows, nav links get their own row, version hidden, reduced container padding) and the settings grids auto-adapt (`repeat(auto-fit, …)`). Verified in a browser at 390px viewport with no horizontal overflow.
      - Files: `templates/base.html`
- [x] **#39 Tooltips/help text** ~~for advanced per-record settings (static vs dynamic, `.local` companion, etc.).~~ **DONE** — `title` tooltips on the Cloudflare DDNS toggle, Dynamic/Static radios, and the UniFi parent/.local toggles in both config panels (`dashboard.html` and `records_table.html`). Verified in the browser.
      - Files: `templates/dashboard.html`, `templates/partials/records_table.html`
- [ ] **#40 Dark mode** (design system is dark-nav/light-body only). **DEFERRED (2026-08-10): nice-to-have, not needed for now — no dark theme currently exists.**

---

## ✨ P3 — Features

- [ ] **#41 Notifications/alerting** — webhook / ntfy / Telegram / email on IP change, update success, or repeated failures. **DEFERRED (2026-08-10): not needed for now.**
- [ ] **#42 IPv6 (AAAA) support** — currently A-records only. **DEFERRED (2026-08-10): not needed for now.**
- [x] **#43 IP-provider fallback chain** ~~— single `api.ipify.org` dependency.~~ **DONE** — `IpService` now tries an ordered chain (`api.ipify.org` → `icanhazip.com` → `ifconfig.me`), rolling over on transient failure and raising `IpFetchError` only when all providers fail. The provider list is injectable so tests stay network-free; fallback unit tests added.
      - Files: `services/ip_service.py`
- [x] **#44 IP change history** ~~— stats keep only counters; add history table + timeline chart.~~ **DONE** — new append-only `IpHistoryEntry` table (record_name, ip, source, timestamp); `HistoryRepository`/`HistoryService`; `DnsService` records every successful transition (`scheduler`/`manual`/`create` source, via the new optional `history_service` collaborator wired in `get_dns_service` + the scheduler) and purges on record delete. New `GET /api/records/{name}/history` fragment + an on-demand "IP change history" timeline in the per-record config modal (dots + IP + timestamp + source, newest first). Unit + integration tests. Verified live in the browser.
      - Files: `db/models.py`, `repositories/history_repository.py`, `services/history_service.py`, `services/dns_service.py`, `dependencies.py`, `scheduler.py`, `routes/api_routes.py`, `templates/dashboard.html`, `templates/partials/record_history.html`
- [x] **#45 Token validation on save** ~~— verify Cloudflare token (`GET /user/tokens/verify`) and auto-list zones in Settings.~~ **DONE** — added `CloudflareClient.verify_token()` and `list_zones()`, a `POST /api/verify-token` route (token in the form body, not a query param), and a "Verify & fetch zones" button in Settings that verifies the token and returns click-to-add zone buttons. Verified live in the browser.
      - Files: `cloudflare/cloudflare_client.py`, `routes/api_routes.py`, `templates/settings.html`
- [x] **#46 Import/export config** ~~(records + settings as JSON) for backup/migration.~~ **DONE** — `GET /api/export` downloads the full config (settings, zones, records, per-record configs) as JSON; `POST /api/import` restores it (including `ConfigService.replace_managed_records`). Settings page gains a Backup/Migration section (export link + JSON file import). Round-trip integration test + unit test. Verified live.
      - Files: `routes/api_routes.py`, `services/config_service.py`, `templates/settings.html`
- [x] **#47 Prometheus metrics endpoint** ~~(per-record counters, cycle duration).~~ **DONE** — new `metrics.py` (dedicated registry) with per-record `ddns_checks_total` / `ddns_updates_total` / `ddns_failures_total` counters and a `ddns_cycle_duration_seconds` histogram; `DnsService` increments them on check/update/failure and times the cycle; `GET /metrics` exposes them in Prometheus exposition format. New pinned dependency `prometheus-client==0.20.0`. Unit + integration tests; verified live.
      - Files: `metrics.py`, `services/dns_service.py`, `app.py`, `requirements.txt`
- [x] **#48 Better `/health`** ~~— liveness (static ok) vs readiness (DB writable, scheduler running), include version.~~ **DONE** — `/health` now includes the version; new `/health/ready` reports `database`, `scheduler`, and `version` with an overall `ok`/`degraded` status. Verified live (`{"status":"ok","database":true,"scheduler":true,"version":"v2.1.5"}`).
- [x] **#49 CSV export** ~~of the log table.~~ **DONE** — `GET /api/logs/export` returns the recent log entries (up to 1000) as a `text/csv` attachment (`ddns-logs-YYYYMMDD.csv`); an "Export CSV" link sits next to "Clear Logs" on the logs page. Integration test + live verification.
      - Files: `routes/api_routes.py`, `templates/logs.html`

---

## 🛠 P3 — DevOps / local tooling

- [x] **#51 Vendor `htmx.min.js` into `static/`** ~~so local dev works without a Docker build (also removes the build-time network dependency).~~ **DONE** — vendored v2.0.4; same fix as #10.
- [x] **#52 Document single-worker constraint** ~~— in-process SSE broadcaster + SQLite mean `--workers 1` is mandatory. Add explicit note/flag in the Docker CMD + README.~~ **DONE** — Docker `CMD` now passes `--workers 1` explicitly with a comment, and the README documents the constraint.
- [ ] **#53 Multi-arch image builds** (`linux/amd64` + `arm64`) for Raspberry Pi / UniFi-style deployments. **DEFERRED (2026-08-10): not needed for now.**

---

## Release checklist (manual, per version)

1. Update `README.md` (pinned tag, Project Status, feature changes)
2. Update `shared_templates.py` — `APP_VERSION`
3. Update `app.py` — `FastAPI(version=...)`
4. Commit + tag `vX.Y.Z`
5. Build/push GHCR images (`vX.Y.Z` and `latest`)
