"""
log_cleanup.py

Responsibility: Determines whether a log cleanup is due and performs the
cleanup by delegating to LogService.
Does NOT: write new log entries, manage DNS records, or read configuration.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import Session

from services.log_service import LogService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cleanup scheduling logic
# ---------------------------------------------------------------------------

# NOTE: We store the last cleanup timestamp in memory. This is sufficient
# because the scheduler runs continuously — a restart resets the timestamp
# but cleanup is idempotent and incurs only a small DB cost.
_last_cleanup_at: datetime | None = None


def should_run_cleanup() -> bool:
    """
    Returns True if at least 24 hours have elapsed since the last cleanup.

    Returns:
        True if cleanup should run, False otherwise.
    """
    if _last_cleanup_at is None:
        return True
    elapsed = datetime.now(timezone.utc) - _last_cleanup_at
    return elapsed.total_seconds() >= 86_400  # 24 hours


def run_cleanup(session: Session, days_to_keep: int = 7) -> int:
    """
    Deletes log entries older than `days_to_keep` days.

    Only runs if should_run_cleanup() returns True; otherwise is a no-op.

    Args:
        session: An active SQLModel Session for DB access.
        days_to_keep: Log entries older than this many days are deleted.

    Returns:
        The number of log entries deleted (0 if cleanup did not run).
    """
    global _last_cleanup_at

    if not should_run_cleanup():
        return 0

    log_service = LogService(session)
    deleted = log_service.delete_older_than(days=days_to_keep)
    _last_cleanup_at = datetime.now(timezone.utc)
    logger.info("Log cleanup complete: %d entries removed.", deleted)
    return deleted
