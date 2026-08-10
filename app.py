"""
app.py

Responsibility: FastAPI application factory. Wires up the database, scheduler,
file watcher, static files, templates, and route handlers. Configures Python
logging once for the entire application.
Does NOT: contain business logic, DNS calls, or direct database queries.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from db.database import init_db
from exceptions import ConfigLoadError, DnsProviderError, IpFetchError
from routes.action_routes import router as action_router
from routes.api_routes import router as api_router
from routes.ui_routes import router as ui_router
from scheduler import create_scheduler
from services.broadcast_service import BroadcastService
from shared_templates import APP_VERSION
from watcher import create_observer

# ---------------------------------------------------------------------------
# Logging — configured ONCE here; all other modules use getLogger(__name__)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifespan — startup and shutdown logic
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manages application startup and graceful shutdown.

    Startup sequence:
        1. Initialise SQLite database (create tables if absent).
        2. Create the shared httpx.AsyncClient.
        3. Start the APScheduler DDNS check job.
        4. Start the watchdog config-volume observer.

    Shutdown sequence:
        1. Stop the APScheduler scheduler.
        2. Stop the watchdog observer.
        3. Close the shared httpx.AsyncClient.

    Args:
        app: The FastAPI application instance.

    Yields:
        None — control returns to FastAPI for the lifetime of the server.
    """
    logger.info("DDNS Dashboard starting up.")

    # 1. Database
    init_db()

    # 2. Shared HTTP client — one connection pool for the whole app
    http_client = httpx.AsyncClient(timeout=10.0)
    app.state.http_client = http_client

    # Shared IP cache — populated by IpService; shared across all requests
    app.state.ip_cache = {"ip": None, "fetched_at": 0.0}

    # Shared listing cache — populated by Cloudflare/UniFi clients so the
    # scheduler cycle and UI page loads share zone/site listings per interval
    app.state.listing_cache = {}

    # SSE broadcaster — fan-out bus for all connected SSE clients
    app.state.broadcaster = BroadcastService()

    # NOTE: UniFi controllers use self-signed certs so a dedicated client with
    # verify=False is kept for all UniFi calls rather than disabling SSL globally.
    # Defense-in-depth: set UNIFI_CA_BUNDLE to a PEM bundle path to verify the
    # controller's certificate instead of disabling verification entirely.
    unifi_verify = os.getenv("UNIFI_CA_BUNDLE", "") or False
    unifi_http_client = httpx.AsyncClient(verify=unifi_verify, timeout=10.0)
    app.state.unifi_http_client = unifi_http_client

    # 3. Scheduler — read initial check interval from DB config
    from sqlmodel import Session

    from db.database import engine
    from repositories.config_repository import ConfigRepository

    with Session(engine) as session:
        config_repo = ConfigRepository(session)
        config = config_repo.load()
        interval = config.interval

    scheduler = create_scheduler(
        http_client=http_client,
        unifi_http_client=unifi_http_client,
        interval_seconds=interval,
        broadcaster=app.state.broadcaster,
        app_state=app,
    )
    scheduler.start()
    app.state.scheduler = scheduler

    # 4. Watchdog observer — monitor /config for out-of-band changes
    watch_path = os.getenv("CONFIG_DIR", "/config")
    # WORKAROUND: Only start watchdog if the path exists; during local dev /config
    # may not exist, so we fall back to the current directory's config/ folder.
    if not os.path.exists(watch_path):
        watch_path = os.path.join(os.getcwd(), "config")
        os.makedirs(watch_path, exist_ok=True)

    observer = create_observer(watch_path=watch_path)
    observer.start()
    app.state.observer = observer

    logger.info("DDNS Dashboard is ready.")

    yield

    # --- Shutdown ---
    logger.info("DDNS Dashboard shutting down.")
    scheduler.shutdown(wait=False)
    observer.stop()
    observer.join(timeout=5)
    await http_client.aclose()
    await unifi_http_client.aclose()
    logger.info("DDNS Dashboard shut down cleanly.")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """
    Creates and configures the FastAPI application.

    Returns:
        A fully configured FastAPI instance.
    """
    application = FastAPI(
        title="Cloudflare DDNS Dashboard",
        description="Monitors public IP and updates Cloudflare DNS A-records automatically.",
        # NOTE: Single source of truth — APP_VERSION (e.g. "v2.2.0") is shared
        # with the UI via shared_templates.py; strip the leading "v" for the
        # numeric version string FastAPI expects.
        version=APP_VERSION.removeprefix("v"),
        lifespan=lifespan,
    )

    # Static files — HTMX served locally; never from CDN in production
    application.mount("/static", StaticFiles(directory="static"), name="static")

    # Routers
    application.include_router(ui_router)
    application.include_router(action_router)
    application.include_router(api_router)

    # ---------------------------------------------------------------------------
    # Health endpoint — used by Docker HEALTHCHECK
    # ---------------------------------------------------------------------------

    @application.get("/health", tags=["ops"])
    def health() -> dict:
        """Liveness probe used by Docker HEALTHCHECK."""
        return {"status": "ok", "version": APP_VERSION}

    @application.get("/health/ready", tags=["ops"])
    def health_ready(request: Request) -> dict:
        """
        Readiness probe — verifies the database and scheduler are operational.

        A degraded status means a component is not ready (e.g. DB unreachable
        or the scheduler has not started), which is useful for orchestration
        systems that gate traffic on readiness.

        Args:
            request: The incoming FastAPI request (for app.state).

        Returns:
            A dict with overall "status" plus per-component flags and version.
        """
        db_ok = True
        try:
            # NOTE: A trivial read proves the SQLite file is reachable and
            # queryable; any DB error means the app cannot operate.
            from sqlmodel import Session, text

            from db.database import engine
            with Session(engine) as session:
                session.exec(text("SELECT 1"))
        except Exception:
            db_ok = False

        scheduler = getattr(request.app.state, "scheduler", None)
        scheduler_ok = scheduler is not None and getattr(scheduler, "running", False)

        ready = db_ok and scheduler_ok
        return {
            "status": "ok" if ready else "degraded",
            "database": db_ok,
            "scheduler": scheduler_ok,
            "version": APP_VERSION,
        }

    # ---------------------------------------------------------------------------
    # Prometheus metrics endpoint — scraped by monitoring systems
    # ---------------------------------------------------------------------------

    @application.get("/metrics", tags=["ops"])
    def metrics() -> PlainTextResponse:
        """
        Exposes Prometheus metrics in the text exposition format.

        Returns:
            A PlainTextResponse with the rendered metric payload.
        """
        from metrics import CONTENT_TYPE_LATEST, render_metrics

        return PlainTextResponse(render_metrics(), media_type=CONTENT_TYPE_LATEST)

    # ---------------------------------------------------------------------------
    # Custom exception handlers — return JSON for domain errors
    # ---------------------------------------------------------------------------

    @application.exception_handler(IpFetchError)
    async def ip_fetch_error_handler(request: Request, exc: IpFetchError) -> JSONResponse:
        logger.error("IpFetchError: %s", exc)
        return JSONResponse(status_code=503, content={"error": "Could not determine public IP.", "detail": str(exc)})

    @application.exception_handler(DnsProviderError)
    async def dns_provider_error_handler(request: Request, exc: DnsProviderError) -> JSONResponse:
        logger.error("DnsProviderError: %s", exc)
        return JSONResponse(status_code=502, content={"error": "DNS provider error.", "detail": str(exc)})

    @application.exception_handler(ConfigLoadError)
    async def config_load_error_handler(request: Request, exc: ConfigLoadError) -> JSONResponse:
        logger.error("ConfigLoadError: %s", exc)
        return JSONResponse(status_code=500, content={"error": "Configuration error.", "detail": str(exc)})

    return application


app = create_app()
