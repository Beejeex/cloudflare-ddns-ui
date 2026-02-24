# Cloudflare DDNS Dashboard

> ⚠️ **Beta Software** — This project is in active development. Expect breaking changes between versions. Use in production at your own risk.

A self-hosted Dynamic DNS dashboard for Cloudflare. Monitors your host machine's public IP address and automatically updates Cloudflare DNS A-records when it changes. Includes a web UI for managing tracked records, viewing update logs, and configuring API credentials — all from a single Docker container with no external dependencies.

---

## Features

- **Automatic IP tracking** — polls a public IP provider on a configurable interval and updates Cloudflare when the IP changes
- **Web UI** — dark/light dashboard built with FastAPI + Jinja2 + HTMX; no page reloads
- **Multi-zone support** — manage A-records across multiple Cloudflare zones from one instance
- **Create & manage records** — create new Cloudflare A-records or track existing ones directly from the UI
- **Live log viewer** — per-update audit log stored in SQLite, visible at `/logs`
- **Settings page** — configure API token and zones via a friendly form (no JSON editing required)
- **Single container** — SQLite database, scheduler, and file watcher all in one `python:3.12-slim` image
- **Health endpoint** — `GET /health` for Docker `HEALTHCHECK` and uptime monitors

---

## Requirements

- Docker (any recent version)
- A Cloudflare account with an API token scoped to `Zone:DNS:Edit` for the zones you want to manage

---

## Quick Start

```bash
docker run -d \
  --name ddns-dashboard \
  --restart unless-stopped \
  -p 8080:8080 \
  -v /path/to/your/config:/config \
  ghcr.io/beejeex/cloudflare-ddns-ui:latest
```

Then open `http://localhost:8080` and go to **Settings** to enter your Cloudflare API token and zone IDs.

### Build locally

```bash
git clone https://github.com/Beejeex/cloudflare-ddns-ui.git
cd cloudflare-ddns-ui
docker build -t ddns-dashboard .
docker run -d \
  --name ddns-dashboard \
  --restart unless-stopped \
  -p 8080:8080 \
  -v "$PWD/config:/config" \
  ddns-dashboard
```

---

## Configuration

All configuration is stored in `/config/ddns.db` (SQLite) inside the container. Mount `/config` as a volume so settings and logs survive container restarts.

| Setting | Description |
|---|---|
| **API Token** | Cloudflare API token with `Zone:DNS:Edit` permission |
| **Zones** | One or more domain → Zone ID pairs (e.g. `example.com` → `abc123...`) |
| **Check Interval** | How often (in seconds) to check for an IP change (default: 300) |
| **Log Retention** | How many days to keep log entries (default: 30) |

No environment variables or config files are required — everything is managed through the Settings page.

---

## Tech Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Web framework | FastAPI |
| Templates | Jinja2 + HTMX |
| HTTP client | httpx (async) |
| Scheduler | APScheduler |
| Database | SQLite via SQLModel |
| Container | python:3.12-slim |

---

## Project Status

| Version | Status |
|---|---|
| `v1.x` | Legacy Flask app — archived |
| `v2.x` | **Current** — FastAPI rewrite, active development |

This is **beta software**. The API, database schema, and configuration format may change between minor versions without a migration path. Pin to a specific image tag in production.

Known limitations in the current beta:
- No authentication on the web UI — do not expose port 8080 to the public internet without a reverse proxy + auth layer
- No HTTPS built-in — terminate TLS at your reverse proxy (nginx, Caddy, Traefik)
- Single-instance only — no HA or clustering support

---

## License

CC BY-NC-SA 4.0 — Free for personal/non-commercial use; modifications must be shared under the same license. See [LICENSE](LICENSE) file.
