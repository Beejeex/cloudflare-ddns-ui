"""
db/database.py

Responsibility: Creates the SQLite engine, session factory, and exposes
create_all() for startup table initialisation.
Does NOT: define table models, run queries, or contain business logic.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

# NOTE: /config is the Docker volume mount point so the DB survives restarts.
# During local dev, the path resolves to config/ddns.db inside the project.
_DB_PATH = os.getenv("DB_PATH", "/config/ddns.db")
_DB_URL = f"sqlite:///{_DB_PATH}"

# check_same_thread=False is required for SQLite + FastAPI's async workers.
engine = create_engine(
    _DB_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_db() -> None:
    """
    Creates all tables defined in SQLModel metadata if they don't exist.

    Called once from the FastAPI lifespan function in app.py.

    Returns:
        None
    """
    # Ensure the config directory exists (needed when running outside Docker)
    db_dir = os.path.dirname(_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    SQLModel.metadata.create_all(engine)
    logger.info("Database initialised at %s", _DB_PATH)


def get_session() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a SQLModel Session for the current request.

    Usage in route handlers:
        session: Session = Depends(get_session)

    The session is automatically closed when the request completes.

    Yields:
        A SQLModel Session bound to the application engine.
    """
    with Session(engine) as session:
        yield session
