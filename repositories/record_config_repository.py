"""
repositories/record_config_repository.py

Responsibility: Provides CRUD access for the RecordConfig table in SQLite.
Does NOT: contain business logic, make HTTP calls, or manage sessions.
"""

from __future__ import annotations

import logging

from sqlmodel import Session, select

from db.models import RecordConfig

logger = logging.getLogger(__name__)


class RecordConfigRepository:
    """
    Reads and writes RecordConfig rows (per-DNS-record DDNS settings).

    Each row is keyed by the record's FQDN. If no row exists for a given name,
    the repository silently returns a default-valued instance (not persisted).

    Collaborators:
        - Session: injected SQLModel session, managed externally
    """

    def __init__(self, session: Session) -> None:
        """
        Initialises the repository with the current DB session.

        Args:
            session: The SQLModel session for this request.
        """
        self._session = session

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    def get(self, record_name: str) -> RecordConfig:
        """
        Returns the RecordConfig for the given FQDN, or a default if absent.

        The returned object is NOT persisted unless save() is called.

        Args:
            record_name: The fully-qualified DNS name.

        Returns:
            A RecordConfig instance (possibly with default values).
        """
        row = self._session.exec(
            select(RecordConfig).where(RecordConfig.record_name == record_name)
        ).first()
        return row if row is not None else RecordConfig(record_name=record_name)

    def get_all(self, record_names: list[str]) -> dict[str, RecordConfig]:
        """
        Returns a mapping of FQDN → RecordConfig for all given record names.

        Missing rows are filled in with default-valued instances so callers
        always receive an entry for every name without extra null checks.

        Args:
            record_names: List of managed FQDNs to look up.

        Returns:
            A dict mapping each FQDN to its RecordConfig (real or default).
        """
        if not record_names:
            return {}
        rows = self._session.exec(
            select(RecordConfig).where(RecordConfig.record_name.in_(record_names))
        ).all()
        result: dict[str, RecordConfig] = {r.record_name: r for r in rows}
        # NOTE: Fill in defaults for any name without a persisted row.
        for name in record_names:
            if name not in result:
                result[name] = RecordConfig(record_name=name)
        return result

    def save(self, config: RecordConfig) -> RecordConfig:
        """
        Persists a RecordConfig row (insert or update).

        Args:
            config: The RecordConfig instance to save.

        Returns:
            The refreshed RecordConfig after commit.
        """
        self._session.add(config)
        self._session.commit()
        self._session.refresh(config)
        return config

    def delete(self, record_name: str) -> None:
        """
        Deletes the RecordConfig row for the given FQDN if it exists.

        Args:
            record_name: The FQDN whose config row should be removed.

        Returns:
            None
        """
        row = self._session.exec(
            select(RecordConfig).where(RecordConfig.record_name == record_name)
        ).first()
        if row is not None:
            self._session.delete(row)
            self._session.commit()
            logger.debug("Deleted RecordConfig for %s", record_name)
