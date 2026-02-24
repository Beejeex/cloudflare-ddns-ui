"""
services/log_service.py

Responsibility: Writes DDNS activity log entries to the database and reads
them back for the UI log panel. Also handles log cleanup.
Does NOT: manage DNS records, fetch IPs, or read application configuration.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from db.models import LogEntry

logger = logging.getLogger(__name__)


class LogService:
    """
    Manages DDNS activity log entries stored in the SQLite LogEntry table.

    These log entries are the ones shown in the UI log panel.  They are
    separate from Python's standard logging infrastructure (which writes to
    stdout / uvicorn's logging pipeline).

    Collaborators:
        - Session: SQLModel DB session injected at construction time
    """

    def __init__(self, session: Session) -> None:
        """
        Initialises the log service with an active DB session.

        Args:
            session: An open SQLModel Session for the current request.
        """
        self._session = session

    # ---------------------------------------------------------------------------
    # Write operations
    # ---------------------------------------------------------------------------

    def log(self, message: str, level: str = "INFO") -> LogEntry:
        """
        Writes a single log entry to the database.

        Also emits the message to Python's standard logging so it appears
        in the uvicorn/container log stream.

        Args:
            message: The human-readable log message.
            level: Log severity string ("INFO", "WARNING", "ERROR").

        Returns:
            The persisted LogEntry instance.
        """
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            level=level.upper(),
            message=message,
        )
        self._session.add(entry)
        self._session.commit()
        self._session.refresh(entry)

        # Mirror to Python logging so the message appears in container stdout
        _level_int = getattr(logging, level.upper(), logging.INFO)
        logger.log(_level_int, message)

        return entry

    # ---------------------------------------------------------------------------
    # Read operations
    # ---------------------------------------------------------------------------

    def get_recent(self, limit: int = 100) -> list[LogEntry]:
        """
        Returns the most recent log entries, newest first.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            A list of LogEntry instances ordered by timestamp descending.
        """
        statement = (
            select(LogEntry)
            .order_by(LogEntry.timestamp.desc())  # type: ignore[arg-type]
            .limit(limit)
        )
        return list(self._session.exec(statement).all())

    def get_by_level(self, level: str, limit: int = 100) -> list[LogEntry]:
        """
        Returns the most recent log entries filtered by severity level.

        Args:
            level: The severity level to filter by (e.g. "ERROR").
            limit: Maximum number of entries to return.

        Returns:
            A list of LogEntry instances matching the level, newest first.
        """
        statement = (
            select(LogEntry)
            .where(LogEntry.level == level.upper())
            .order_by(LogEntry.timestamp.desc())  # type: ignore[arg-type]
            .limit(limit)
        )
        return list(self._session.exec(statement).all())

    # ---------------------------------------------------------------------------
    # Cleanup
    # ---------------------------------------------------------------------------

    def delete_older_than(self, days: int) -> int:
        """
        Deletes all log entries older than the given number of days.

        Args:
            days: Entries older than this many days will be deleted.

        Returns:
            The number of entries deleted.
        """
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        statement = select(LogEntry).where(LogEntry.timestamp < cutoff)
        old_entries = list(self._session.exec(statement).all())

        for entry in old_entries:
            self._session.delete(entry)

        self._session.commit()
        logger.info("Log cleanup: deleted %d entries older than %d days.", len(old_entries), days)
        return len(old_entries)
