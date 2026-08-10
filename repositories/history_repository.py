"""
repositories/history_repository.py

Responsibility: Provides low-level read/write access to the IpHistoryEntry
(per-record IP change history) table in SQLite via SQLModel.
Does NOT: contain business logic, DNS logic, or log parsing.
"""

from __future__ import annotations

import logging

from sqlmodel import Session, select

from db.models import IpHistoryEntry
from utils import utcnow_naive

logger = logging.getLogger(__name__)


class HistoryRepository:
    """
    Manages persistence of per-record IP change history entries.

    The table is append-only: each successful IP transition inserts a new row
    so the UI can render a timeline of changes for a record.

    Collaborators:
        - Session: SQLModel DB session injected at construction time
    """

    def __init__(self, session: Session) -> None:
        """
        Initialises the repository with an active DB session.

        Args:
            session: An open SQLModel Session for the current request.
        """
        self._session = session

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    def add(
        self,
        record_name: str,
        ip: str,
        source: str = "scheduler",
    ) -> IpHistoryEntry:
        """
        Inserts a new IP change history entry.

        Args:
            record_name: The fully-qualified DNS name that changed.
            ip: The IP the record was set to.
            source: Who triggered the change ("scheduler", "manual", "create").

        Returns:
            The persisted IpHistoryEntry instance.
        """
        entry = IpHistoryEntry(
            record_name=record_name,
            ip=ip,
            source=source,
            timestamp=utcnow_naive(),
        )
        self._session.add(entry)
        self._session.commit()
        self._session.refresh(entry)
        logger.debug("History: %s → %s (%s)", record_name, ip, source)
        return entry

    def get_for_record(
        self,
        record_name: str,
        limit: int = 50,
    ) -> list[IpHistoryEntry]:
        """
        Returns the most recent IP changes for a single record, newest first.

        Args:
            record_name: The fully-qualified DNS name to look up.
            limit: Maximum number of entries to return.

        Returns:
            A list of IpHistoryEntry instances ordered by timestamp descending.
        """
        statement = (
            select(IpHistoryEntry)
            .where(IpHistoryEntry.record_name == record_name)
            .order_by(IpHistoryEntry.timestamp.desc())  # type: ignore[arg-type]
            .limit(limit)
        )
        return list(self._session.exec(statement).all())

    def delete_for_record(self, record_name: str) -> int:
        """
        Removes all history rows for the given record.

        Called when a record is removed from the managed list so stale history
        is not shown in the UI.

        Args:
            record_name: The fully-qualified DNS name to purge.

        Returns:
            The number of rows deleted.
        """
        statement = select(IpHistoryEntry).where(
            IpHistoryEntry.record_name == record_name
        )
        rows = list(self._session.exec(statement).all())
        for row in rows:
            self._session.delete(row)
        if rows:
            self._session.commit()
        return len(rows)