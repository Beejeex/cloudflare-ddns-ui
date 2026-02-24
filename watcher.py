"""
watcher.py

Responsibility: Sets up a watchdog file system observer that monitors the
/config directory for out-of-band changes and logs relevant events.
Does NOT: contain DNS business logic, config parsing, or scheduler logic.
"""

from __future__ import annotations

import logging

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event handler
# ---------------------------------------------------------------------------


class _ConfigDirectoryHandler(FileSystemEventHandler):
    """
    Watchdog event handler for the /config volume directory.

    Logs modifications to the config directory so operators are aware of
    out-of-band changes (e.g. manual edits to the DB file on the volume).
    The application re-reads config from DB on every request, so no active
    reload is needed — the log is informational only.
    """

    def on_modified(self, event: FileSystemEvent) -> None:
        """
        Called by watchdog when a file in the watched directory is modified.

        Args:
            event: The file system event describing what changed.

        Returns:
            None
        """
        if event.is_directory:
            return
        logger.info("Config volume change detected: %s", event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        """
        Called by watchdog when a new file is created in the watched directory.

        Args:
            event: The file system event describing what was created.

        Returns:
            None
        """
        if event.is_directory:
            return
        logger.debug("New file in config volume: %s", event.src_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_observer(watch_path: str = "/config") -> Observer:
    """
    Creates and returns a configured (but not yet started) watchdog Observer.

    Args:
        watch_path: The directory path to monitor. Defaults to /config
                    which is the Docker volume mount point.

    Returns:
        A configured watchdog Observer ready to be started.
    """
    observer = Observer()
    handler = _ConfigDirectoryHandler()
    observer.schedule(handler, path=watch_path, recursive=False)
    logger.info("File watcher configured for: %s", watch_path)
    return observer
