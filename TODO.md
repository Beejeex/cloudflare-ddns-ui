# TODO — Cloudflare DNS Dashboard

> Local-only development. **No** GitHub Actions, no automated builds, no CI/CD.
> All improvements are tracked here and implemented locally.
> Current version: **v2.1.5**

Check off items with `[x]` as they are completed.

---

## 🔴 P1 — Bugs & correctness

- [ ] **#1 Log retention setting missing.** README documents "Log Retention (default 30 days)" but there is no such setting — `scheduler.py` hardcodes `run_cleanup(session, days_to_keep=7)`. Add a real config field + Settings UI, or fix the docs.
      - Files: `scheduler.py`, `db/models.py`, `routes/ui_routes.py`, `templates/settings.html`, `README.md`
- [ ] **#2 Cloudflare `list_records()` has no pagination.** Calls `GET /dns_records?type=A` without `per_page` (CF default = 20). Zones with >20 A-records silently miss records in the discovery panel and `fetch_zone_record_map()`.
      - Files: `cloudflare/cloudflare_client.py`
- [ ] **#2b UniFi `list_records()` only fetches first 200** (`offset=0, limit=200`), no pagination loop.
      - Files: `cloudflare/unifi_client.py`
- [ ] **#3 "Up to date" logic wrong for static-IP records.** `is_up_to_date = (dns_ip == current_ip)` compares against the dynamic public IP even when `ip_mode="static"`. Should compare against `cfg.static_ip`.
      - Files: `routes/ui_routes.py`, `routes/api_routes.py`
- [ ] **#6 Race condition on managed-records list.** Records are a JSON array in the single `AppConfig.records_json` row — read-modify-write can lose entries under concurrent tabs.
      - Files: `services/config_service.py`, `repositories/config_repository.py`
- [ ] **#7 `_to_local_policy_name` duplicated in 3 files.** `scheduler.py`, `routes/ui_routes.py`, `routes/api_routes.py` — extract to one shared module (e.g. `shared_templates.py` or new `utils.py`).
- [ ] **#8 SSE broadcast re-fetches public IP upstream every cycle.** `IpService` used in `_ddns_check_job` has no `app_state` so caching is disabled. Reuse the cached IP from the cycle.
      - Files: `scheduler.py`
- [ ] **#9 `/api/next-check-in` swallows DB errors (`except Exception: pass`)** and builds repos inline instead of via `Depends()`.
      - Files: `routes/api_routes.py`
- [ ] **#10 `htmx.min.js` not vendored.** `static/` only has `alpinejs.min.js` + `htmx-sse.js`; htmx is downloaded at Docker build. Local (non-Docker) dev 404s on `/static/htmx.min.js`.
- [ ] **#11 Mixed/naive datetimes.** `LogEntry` uses `datetime.utcnow()` (deprecated in 3.12); `LogService` strips tzinfo; stats timestamps naive. Standardize on timezone-aware UTC.
      - Files: `db/models.py`, `services/log_service.py`
- [ ] **#12 No retry on transient HTTP failures** for IP provider / Cloudflare — one network blip fails the whole cycle.
      - Files: `services/ip_service.py`, `cloudflare/cloudflare_client.py`

---

## 🔐 P2 — Security (internal app — auth intentionally OUT OF SCOPE)

> **Decision (2026-08-08):** This is an internal, trusted-network app only. Authentication, CSRF, and rate limiting are **deliberately not planned**. Do not re-add them. Only defense-in-depth / hygiene items below.

- [ ] **#5 Secrets exposed in plaintext.** Full `api_token` rendered into `settings.html` value attributes; `/api/unifi/sites` takes the UniFi API key as a query param (logged by proxies). Mask tokens (last 4 chars + "replace" semantics) and move the key to form body/header.
      - Files: `templates/settings.html`, `routes/api_routes.py`
- [ ] **#14 UniFi CA options.** `verify=False` is required for self-signed, but add optional CA bundle / pinned cert instead of disabling verification entirely.
      - Files: `cloudflare/unifi_client.py`
- [ ] **#16 Secrets at rest in plaintext** in SQLite. Document `/config` permissions; consider masking/encryption.

---

## 🏗 P2 — Architecture & code quality

- [ ] **#17 `_ddns_check_job` is a god-function.** Does CF cycle + UniFi pass + log cleanup + template rendering for SSE inline. Extract a `UniFiService` and a broadcast/notifier helper so the job only triggers.
      - Files: `scheduler.py`
- [ ] **#18 Record-row dict building duplicated ~4×.** `_build_record_rows` (`action_routes.py`), scheduler SSE render, `_render_records_for_sse`, `get_records` (`api_routes.py`). One shared presenter or a typed `RecordRow` model.
- [ ] **#19 DI is inconsistent.** Routes construct `IpService(request.app.state.http_client)` and repos inline instead of `Depends()` providers.
      - Files: `routes/ui_routes.py`, `routes/api_routes.py`
- [ ] **#20 Dead code to remove.** `_render_records_for_sse` (never called), `ui_state` config surface (`get_ui_state`/`set_ui_state` — unused), `kubeconfig_path` DB column (not in model, never read).
      - Files: `routes/api_routes.py`, `services/config_service.py`, `repositories/config_repository.py`, `db/database.py`
- [ ] **#21 Mid-file imports with `# noqa: E402`.** `from shared_templates import templates` inside route files — move to top.
      - Files: `routes/action_routes.py`, `routes/api_routes.py`, `routes/ui_routes.py`
- [ ] **#22 Version string in two places.** `app.py` `"2.1.5"` and `shared_templates.py` `"v2.1.5"` — single constant would remove mismatch risk.
- [ ] **#23 Broad `except Exception` blocks.** SSE generator, scheduler broadcast — narrow to specific exceptions where possible.

---

## ⚡ P2 — Performance

- [ ] **#24 Dashboard page load is sequential** — IP, UniFi, CF zones, K8s all awaited one-by-one. Wrap in `asyncio.gather()`.
      - Files: `routes/ui_routes.py`
- [ ] **#25 No cache of CF/UniFi listings** between scheduler and UI — every page load / SSE reconnect re-fetches all zones and policies.
- [ ] **#26 No index on `LogEntry.timestamp`** — `ORDER BY timestamp DESC` slows down as the log table grows.
      - Files: `db/database.py` (migration) or `db/models.py`
- [ ] **#27 Check cycle does many small commits** (per record) — batch stats/log writes.

---

## 🧪 P2 — Testing

- [ ] **#29 No tests for the scheduler** (`_ddns_check_job`, `run_ddns_check_now`, `reschedule`), watcher, or `log_cleanup`.
      - Files: `tests/unit/`
- [ ] **#30 Coverage gaps.** No tests for pagination, static-mode "up to date" logic, SSE stream, or `/create-record` failure path.
- [ ] **#31 No coverage threshold enforced** despite `pytest-cov` being installed.
      - Files: `pytest.ini`
- [ ] **#32 `requirements.txt` unpinned** (bare `fastapi`, `httpx`, …) — pin versions for reproducibility.
      - Files: `requirements.txt`

---

## 🎨 P2 — UI/UX

- [ ] **#33 Empty states / onboarding wizard** — first-run setup flow (enter token → verify → pick zones → add first record).
      - Files: `templates/dashboard.html`, `routes/ui_routes.py`
- [ ] **#34 Logs page has no filtering/pagination** — only latest 100, no filter by level/record, no "load more". `LogService.get_by_level` exists but isn't exposed.
      - Files: `templates/logs.html`, `routes/api_routes.py`
- [ ] **#35 No per-record "check now / force update"** — only the global `/trigger-sync`.
- [ ] **#36 No bulk actions** (enable/disable CF or UniFi for all records at once).
- [ ] **#37 Accessibility** — toggles lack `aria-label`s, no visible focus states, small targets.
      - Files: `templates/base.html`, `templates/dashboard.html`
- [ ] **#38 Mobile responsiveness** — navbar `min-width: 220px` both sides, no mobile menu, grid likely overflows.
      - Files: `templates/base.html`
- [ ] **#39 Tooltips/help text** for advanced per-record settings (static vs dynamic, `.local` companion, etc.).
- [ ] **#40 Dark mode** (design system is dark-nav/light-body only).

---

## ✨ P3 — Features

- [ ] **#41 Notifications/alerting** — webhook / ntfy / Telegram / email on IP change, update success, or repeated failures.
- [ ] **#42 IPv6 (AAAA) support** — currently A-records only.
- [ ] **#43 IP-provider fallback chain** — single `api.ipify.org` dependency; add `icanhazip.com` / `ifconfig.me` fallbacks.
      - Files: `services/ip_service.py`
- [ ] **#44 IP change history** — stats keep only counters; add history table + timeline chart.
- [ ] **#45 Token validation on save** — verify Cloudflare token (`GET /user/tokens/verify`) and auto-list zones in Settings.
- [ ] **#46 Import/export config** (records + settings as JSON) for backup/migration.
- [ ] **#47 Prometheus metrics endpoint** (per-record counters, cycle duration).
- [ ] **#48 Better `/health`** — liveness (static ok) vs readiness (DB writable, scheduler running), include version.
- [ ] **#49 CSV export** of the log table.

---

## 🛠 P3 — DevOps / local tooling

- [ ] **#51 Vendor `htmx.min.js` into `static/`** so local dev works without a Docker build (also removes the build-time network dependency).
- [ ] **#52 Document single-worker constraint** — in-process SSE broadcaster + SQLite mean `--workers 1` is mandatory. Add explicit note/flag in the Docker CMD + README.
- [ ] **#53 Multi-arch image builds** (`linux/amd64` + `arm64`) for Raspberry Pi / UniFi-style deployments.

---

## Release checklist (manual, per version)

1. Update `README.md` (pinned tag, Project Status, feature changes)
2. Update `shared_templates.py` — `APP_VERSION`
3. Update `app.py` — `FastAPI(version=...)`
4. Commit + tag `vX.Y.Z`
5. Build/push GHCR images (`vX.Y.Z` and `latest`)
