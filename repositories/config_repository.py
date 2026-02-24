"""
repositories/config_repository.py

Responsibility: Provides low-level read/write access to the AppConfig table
in SQLite via SQLModel.
Does NOT: contain business logic, IP fetching, or UI concerns.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlmodel import Session, select

from db.models import AppConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default values — single source of truth for all config keys
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "api_token": "",
    "zones": {},
    "records": [],
    "refresh": 30,
    "interval": 300,
    "ui_state": {"settings": True, "all_records": True, "logs": True},
}


class ConfigRepository:
    """
    Manages persistence of the single AppConfig row in the database.

    Reads and writes the AppConfig table. JSON-serialised fields (zones,
    records, ui_state) are encoded/decoded here so callers always receive
    plain Python types.

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

    def load(self) -> AppConfig:
        """
        Returns the single AppConfig row, creating it with defaults if absent.

        Returns:
            The AppConfig ORM instance (never None).
        """
        statement = select(AppConfig)
        config = self._session.exec(statement).first()

        if config is None:
            logger.info("No AppConfig row found — seeding defaults.")
            config = AppConfig(
                api_token=_DEFAULTS["api_token"],
                zones_json=json.dumps(_DEFAULTS["zones"]),
                records_json=json.dumps(_DEFAULTS["records"]),
                refresh=_DEFAULTS["refresh"],
                interval=_DEFAULTS["interval"],
                ui_state_json=json.dumps(_DEFAULTS["ui_state"]),
            )
            self._session.add(config)
            self._session.commit()
            self._session.refresh(config)

        return config

    def save(self, config: AppConfig) -> AppConfig:
        """
        Persists an AppConfig instance to the database.

        Args:
            config: The AppConfig instance to save. May be new or existing.

        Returns:
            The refreshed AppConfig instance after commit.
        """
        self._session.add(config)
        self._session.commit()
        self._session.refresh(config)
        logger.debug("AppConfig saved (id=%s).", config.id)
        return config

    # ---------------------------------------------------------------------------
    # Convenience accessors — encode/decode JSON fields
    # ---------------------------------------------------------------------------

    def get_zones(self, config: AppConfig) -> dict[str, str]:
        """
        Decodes the zones_json field into a Python dict.

        Args:
            config: The AppConfig instance to read from.

        Returns:
            A dict mapping base domain strings to Cloudflare zone IDs.
        """
        try:
            return json.loads(config.zones_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning("zones_json is corrupt; returning empty dict.")
            return {}

    def set_zones(self, config: AppConfig, zones: dict[str, str]) -> None:
        """
        Encodes and stores the zones dict into the zones_json field.

        Args:
            config: The AppConfig instance to modify (in place).
            zones: A dict mapping base domain strings to Cloudflare zone IDs.

        Returns:
            None
        """
        config.zones_json = json.dumps(zones)

    def get_records(self, config: AppConfig) -> list[str]:
        """
        Decodes the records_json field into a Python list of FQDNs.

        Args:
            config: The AppConfig instance to read from.

        Returns:
            A list of fully-qualified DNS names being tracked.
        """
        try:
            return json.loads(config.records_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning("records_json is corrupt; returning empty list.")
            return []

    def set_records(self, config: AppConfig, records: list[str]) -> None:
        """
        Encodes and stores the records list into the records_json field.

        Args:
            config: The AppConfig instance to modify (in place).
            records: A list of fully-qualified DNS names to track.

        Returns:
            None
        """
        config.records_json = json.dumps(records)

    def get_ui_state(self, config: AppConfig) -> dict[str, bool]:
        """
        Decodes the ui_state_json field into a Python dict.

        Args:
            config: The AppConfig instance to read from.

        Returns:
            A dict of section-name to boolean visibility flags.
        """
        try:
            return json.loads(config.ui_state_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning("ui_state_json is corrupt; returning defaults.")
            return dict(_DEFAULTS["ui_state"])

    def set_ui_state(self, config: AppConfig, ui_state: dict[str, bool]) -> None:
        """
        Encodes and stores the ui_state dict into the ui_state_json field.

        Args:
            config: The AppConfig instance to modify (in place).
            ui_state: A dict of section-name to boolean visibility flags.

        Returns:
            None
        """
        config.ui_state_json = json.dumps(ui_state)
