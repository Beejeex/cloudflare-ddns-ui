"""
services/config_service.py

Responsibility: Provides a clean, business-level API for reading and writing
application configuration. Delegates all persistence to ConfigRepository.
Does NOT: make HTTP calls, manage DNS records, or interact with the scheduler.
"""

from __future__ import annotations

import logging

from db.models import AppConfig
from repositories.config_repository import ConfigRepository

logger = logging.getLogger(__name__)


class ConfigService:
    """
    High-level API for reading and writing application configuration.

    Abstracts the JSON encoding/decoding details of ConfigRepository and
    provides intent-named methods such as add_managed_record() that route
    handlers can call directly.

    Collaborators:
        - ConfigRepository: handles all database access
    """

    def __init__(self, config_repo: ConfigRepository) -> None:
        """
        Initialises the service with a config repository.

        Args:
            config_repo: An initialised ConfigRepository for the current session.
        """
        self._repo = config_repo

    # ---------------------------------------------------------------------------
    # Read operations
    # ---------------------------------------------------------------------------

    async def get_config(self) -> AppConfig:
        """
        Returns the current AppConfig row (creating defaults if absent).

        Returns:
            The AppConfig ORM instance.
        """
        return self._repo.load()

    async def get_api_token(self) -> str:
        """
        Returns the stored Cloudflare API token.

        Returns:
            The API token string, or an empty string if not configured.
        """
        config = self._repo.load()
        return config.api_token

    async def get_zones(self) -> dict[str, str]:
        """
        Returns the configured DNS zones mapping.

        Returns:
            A dict mapping base domain strings to Cloudflare zone IDs,
            e.g. {"example.com": "zone_id_abc123"}.
        """
        config = self._repo.load()
        return self._repo.get_zones(config)

    async def get_managed_records(self) -> list[str]:
        """
        Returns the list of DNS record FQDNs currently being managed.

        Returns:
            A list of fully-qualified DNS names, e.g. ["home.example.com"].
        """
        config = self._repo.load()
        return self._repo.get_records(config)

    async def get_refresh_interval(self) -> int:
        """
        Returns the UI auto-refresh interval in seconds.

        Returns:
            The interval as an integer number of seconds.
        """
        config = self._repo.load()
        return config.refresh

    async def get_check_interval(self) -> int:
        """
        Returns the background DDNS check interval in seconds.

        Returns:
            The interval as an integer number of seconds.
        """
        config = self._repo.load()
        return config.interval

    async def get_ui_state(self) -> dict[str, bool]:
        """
        Returns the UI section visibility state.

        Returns:
            A dict of section-name to boolean visibility flags.
        """
        config = self._repo.load()
        return self._repo.get_ui_state(config)

    # ---------------------------------------------------------------------------
    # Write operations
    # ---------------------------------------------------------------------------

    async def update_credentials(
        self,
        api_token: str,
        zones: dict[str, str],
        refresh: int,
        interval: int,
    ) -> AppConfig:
        """
        Saves new Cloudflare credentials and timing configuration.

        Args:
            api_token: The Cloudflare API token with DNS edit permissions.
            zones: A dict mapping base domain strings to Cloudflare zone IDs.
            refresh: UI auto-refresh interval in seconds.
            interval: Background DDNS check interval in seconds.

        Returns:
            The saved AppConfig instance.
        """
        config = self._repo.load()
        config.api_token = api_token
        self._repo.set_zones(config, zones)
        config.refresh = refresh
        config.interval = interval
        self._repo.save(config)
        logger.info("Credentials and intervals updated.")
        return config

    async def add_managed_record(self, record_name: str) -> bool:
        """
        Adds a DNS record FQDN to the managed list if not already present.

        Args:
            record_name: The fully-qualified DNS name to add.

        Returns:
            True if the record was added, False if it was already in the list.
        """
        config = self._repo.load()
        records = self._repo.get_records(config)

        if record_name in records:
            return False

        records.append(record_name)
        self._repo.set_records(config, records)
        self._repo.save(config)
        logger.info("Added managed record: %s", record_name)
        return True

    async def remove_managed_record(self, record_name: str) -> bool:
        """
        Removes a DNS record FQDN from the managed list.

        Args:
            record_name: The fully-qualified DNS name to remove.

        Returns:
            True if the record was removed, False if it was not in the list.
        """
        config = self._repo.load()
        records = self._repo.get_records(config)

        if record_name not in records:
            return False

        records.remove(record_name)
        self._repo.set_records(config, records)
        self._repo.save(config)
        logger.info("Removed managed record: %s", record_name)
        return True

    async def set_ui_state(self, ui_state: dict[str, bool]) -> None:
        """
        Persists the UI section visibility state.

        Args:
            ui_state: A dict mapping section names to visibility booleans.

        Returns:
            None
        """
        config = self._repo.load()
        self._repo.set_ui_state(config, ui_state)
        self._repo.save(config)
