"""
services/history_service.py

Responsibility: Provides a business-level API for recording and retrieving
per-record IP change history. Delegates all persistence to HistoryRepository.
Does NOT: make HTTP calls, update DNS records, or manage stats.
"""

from __future__ import annotations

import logging

from db.models import IpHistoryEntry
from repositories.history_repository import HistoryRepository

logger = logging.getLogger(__name__)


class HistoryService:
    """
    Records and retrieves the IP change timeline for managed DNS records.

    Wraps HistoryRepository with intent-named methods called by DnsService
    after each successful IP transition.

    Collaborators:
        - HistoryRepository: handles all database access for IpHistoryEntry rows
    """

    def __init__(self, history_repo: HistoryRepository) -> None:
        """
        Initialises the service with a history repository.

        Args:
            history_repo: An initialised HistoryRepository for the current session.
        """
        self._repo = history_repo

    def record_ip_change(
        self,
        record_name: str,
        ip: str,
        source: str = "scheduler",
    ) -> IpHistoryEntry:
        """
        Records an IP transition for the given DNS record.

        Args:
            record_name: The fully-qualified DNS name that changed.
            ip: The IP the record was set to.
            source: Who triggered the change ("scheduler", "manual", "create").

        Returns:
            The persisted IpHistoryEntry instance.
        """
        return self._repo.add(record_name, ip, source=source)

    def get_history(
        self,
        record_name: str,
        limit: int = 50,
    ) -> list[IpHistoryEntry]:
        """
        Returns the most recent IP changes for a record, newest first.

        Args:
            record_name: The fully-qualified DNS name to look up.
            limit: Maximum number of entries to return.

        Returns:
            A list of IpHistoryEntry instances ordered by timestamp descending.
        """
        return self._repo.get_for_record(record_name, limit=limit)

    def delete_for_record(self, record_name: str) -> int:
        """
        Purges all history rows for a record.

        Args:
            record_name: The fully-qualified DNS name to purge.

        Returns:
            The number of rows deleted.
        """
        return self._repo.delete_for_record(record_name)