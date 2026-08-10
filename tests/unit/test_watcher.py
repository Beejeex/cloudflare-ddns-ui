"""
tests/unit/test_watcher.py

Unit tests for watcher.py (watchdog observer setup and event handling).
"""

from __future__ import annotations

import logging

from watchdog.events import FileSystemEvent
from watchdog.observers import Observer

from watcher import _ConfigDirectoryHandler, create_observer


def _event(path: str, is_dir: bool) -> FileSystemEvent:
    """Builds a minimal FileSystemEvent with the given directory flag."""
    ev = FileSystemEvent(path)
    ev.is_directory = is_dir
    return ev


def test_create_observer_returns_observer():
    """create_observer must return a watchdog Observer that starts and stops cleanly."""
    observer = create_observer(watch_path="/tmp")
    assert isinstance(observer, Observer)
    observer.start()
    observer.stop()
    observer.join(timeout=2)


def test_handler_ignores_directory_events():
    """The config handler must ignore directory events without raising."""
    handler = _ConfigDirectoryHandler()
    handler.on_modified(_event("/tmp/config", is_dir=True))
    handler.on_created(_event("/tmp/config", is_dir=True))


def test_handler_logs_file_modifications(caplog):
    """The config handler must log a file modification in the config volume."""
    handler = _ConfigDirectoryHandler()
    with caplog.at_level(logging.INFO):
        handler.on_modified(_event("/tmp/config/ddns.db", is_dir=False))
    assert "Config volume change detected" in caplog.text
