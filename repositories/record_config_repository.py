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

    # ---------------------------------------------------------------------------
    # Bulk operations
    # ---------------------------------------------------------------------------

    def set_flag_all(self, record_names: list[str], flag: str, enabled: bool) -> int:
        """
        Sets a RecordConfig boolean flag on every listed record in one commit.

        Rows are upserted: existing rows are updated in place, missing rows are
        created with defaults plus the flag.  A single commit covers the whole
        batch so the operation is one transaction, not N.

        Args:
            record_names: List of managed FQDNs to update.
            flag: The RecordConfig field name to set (e.g. "cf_enabled").
            enabled: The boolean value to write.

        Returns:
            The number of records updated.

        Raises:
            ValueError: If the flag is not a known boolean RecordConfig field.
        """
        if flag not in {"cf_enabled", "unifi_enabled", "unifi_local_enabled"}:
            raise ValueError(f"Unsupported RecordConfig flag: {flag}")
        if not record_names:
            return 0

        existing = self._session.exec(
            select(RecordConfig).where(RecordConfig.record_name.in_(record_names))
        ).all()
        by_name = {row.record_name: row for row in existing}

        for name in record_names:
            config = by_name.get(name)
            if config is None:
                config = RecordConfig(record_name=name)
                self._session.add(config)
            setattr(config, flag, enabled)

        self._session.commit()
        return len(record_names)

    def set_cf_enabled_all(self, record_names: list[str], enabled: bool) -> int:
        """
        Enables or disables Cloudflare DDNS for every listed record at once.

        Args:
            record_names: List of managed FQDNs to update.
            enabled: Whether Cloudflare DDNS should be on or off.

        Returns:
            The number of records updated.
        """
        return self.set_flag_all(record_names, "cf_enabled", enabled)

    def set_unifi_enabled_all(self, record_names: list[str], enabled: bool) -> int:
        """
        Enables or disables UniFi DNS management for every listed record at once.

        Disabling also clears the ``.local`` companion flag so the scheduler's
        sync pass deletes those policies on the next cycle — mirroring what the
        per-record toggle does.

        Args:
            record_names: List of managed FQDNs to update.
            enabled: Whether UniFi DNS management should be on or off.

        Returns:
            The number of records updated.
        """
        count = self.set_flag_all(record_names, "unifi_enabled", enabled)
        if not enabled:
            self.set_flag_all(record_names, "unifi_local_enabled", False)
        return count
