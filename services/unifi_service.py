"""
services/unifi_service.py

Responsibility: Orchestrates the UniFi DNS policy sync pass — creates,
updates, and deletes UniFi DNS policies to match the managed records'
per-record settings, and writes a summary log entry.
Does NOT: run Cloudflare DDNS cycles, schedule jobs, or read configuration.
"""

from __future__ import annotations

import logging

from cloudflare.dns_provider import DnsRecord
from cloudflare.unifi_client import UnifiClient
from db.models import RecordConfig
from exceptions import UnifiProviderError
from repositories.stats_repository import StatsRepository
from services.log_service import LogService
from utils import to_local_policy_name

logger = logging.getLogger(__name__)


class UniFiService:
    """
    Runs the UniFi DNS policy sync pass for all managed records.

    For every managed record the pass reconciles two policies against its
    per-record config:
      - the main domain policy  (controlled by ``RecordConfig.unifi_enabled``)
      - the ".local" companion  (controlled by ``RecordConfig.unifi_local_enabled``)

    All existing policies are fetched once up front (a single GET) and then
    diffed per record — this avoids a burst of per-record list calls that the
    controller answers with 502s.

    Collaborators:
        - UnifiClient: DNSProvider implementation for the UniFi controller
        - LogService: writes UI-visible activity log entries
        - StatsRepository: stamps last_checked on successfully synced records
    """

    def __init__(
        self,
        unifi_client: UnifiClient,
        log_service: LogService,
        stats_repo: StatsRepository,
    ) -> None:
        """
        Initialises the service with its collaborators.

        Args:
            unifi_client: The UniFi DNSProvider implementation.
            log_service: Writes UI-visible activity log entries.
            stats_repo: Stamps last_checked on successfully synced records.
        """
        self._client = unifi_client
        self._log = log_service
        self._stats = stats_repo
        # Per-pass outcome counters, reset at the start of every sync_policies()
        self._created = self._updated = self._unchanged = self._deleted = self._failed = 0

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    async def sync_policies(
        self,
        records: list[str],
        record_configs: dict[str, RecordConfig],
        site_id: str,
        default_ip: str,
        host: str = "",
    ) -> None:
        """
        Reconciles UniFi DNS policies for all managed records.

        Args:
            records: List of managed FQDNs.
            record_configs: Per-record settings keyed by FQDN (defaults filled in).
            site_id: The UniFi site UUID used as the policy zone.
            default_ip: Global fallback IP for policies without a per-record IP.
            host: Optional controller host used only in the summary log line.

        Returns:
            None
        """
        enabled_records = [r for r in records if (rc := record_configs.get(r)) and rc.unifi_enabled]
        host_suffix = f" to {host}" if host else ""
        self._log.log(
            f"UniFi pass: syncing {len(enabled_records)} of {len(records)} record(s){host_suffix}.",
            level="INFO",
        )

        # NOTE: Fetch ALL existing policies in ONE call here, then do dict
        # lookups per record. Without this, each get_record() triggers a
        # separate GET /dns/policies, causing a burst that returns 502s.
        try:
            all_policies = await self._client.list_records(site_id)
            existing_policies: dict[str, DnsRecord] = {p.name: p for p in all_policies}
        except UnifiProviderError as exc:
            self._log.log(f"UniFi pass: could not list policies — {exc}", level="ERROR")
            logger.error("UniFi list_records failed, aborting pass: %s", exc)
            return

        self._created = self._updated = self._unchanged = self._deleted = self._failed = 0

        for record_name in records:
            cfg = record_configs.get(record_name)
            await self._sync_main_policy(record_name, cfg, site_id, default_ip, existing_policies)
            await self._sync_local_policy(record_name, cfg, site_id, default_ip, existing_policies)

        summary_parts: list[str] = []
        if self._unchanged:
            summary_parts.append(f"{self._unchanged} in sync")
        if self._created:
            summary_parts.append(f"{self._created} created")
        if self._updated:
            summary_parts.append(f"{self._updated} updated")
        if self._deleted:
            summary_parts.append(f"{self._deleted} removed")
        if self._failed:
            summary_parts.append(f"{self._failed} failed")
        self._log.log(
            "UniFi pass complete: " + (", ".join(summary_parts) if summary_parts else "nothing to do") + ".",
            level="INFO" if not self._failed else "WARNING",
        )

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    async def _sync_main_policy(
        self,
        record_name: str,
        cfg: RecordConfig | None,
        site_id: str,
        default_ip: str,
        existing_policies: dict[str, DnsRecord],
    ) -> None:
        """
        Reconciles the main domain policy, controlled solely by cfg.unifi_enabled.

        Args:
            record_name: The managed FQDN.
            cfg: The record's per-record config (or None for defaults).
            site_id: The UniFi site UUID.
            default_ip: Global fallback IP.
            existing_policies: Pre-fetched name → policy map.

        Returns:
            None
        """
        if cfg is None or not cfg.unifi_enabled:
            await self._remove_policy(record_name, site_id, existing_policies, label="")
            return
        target_ip = cfg.unifi_static_ip.strip() or default_ip.strip()
        if not target_ip:
            self._log.log(f"UniFi: skipped '{record_name}' — no IP configured.", level="WARNING")
            self._failed += 1
            return
        if await self._ensure_policy(record_name, site_id, target_ip, existing_policies, label=""):
            # NOTE: Stamp last_checked so CF-disabled records always show
            # a timestamp on the dashboard, not just CF-enabled ones.
            self._stats.record_check(record_name)

    async def _sync_local_policy(
        self,
        record_name: str,
        cfg: RecordConfig | None,
        site_id: str,
        default_ip: str,
        existing_policies: dict[str, DnsRecord],
    ) -> None:
        """
        Reconciles the ".local" companion policy, controlled by cfg.unifi_local_enabled.

        NOTE: Independent of unifi_enabled — a .local-only setup
        (unifi_enabled=False + unifi_local_enabled=True) is valid. If the
        managed record itself is already *.local there is no separate
        secondary name to manage.

        Args:
            record_name: The managed FQDN.
            cfg: The record's per-record config (or None for defaults).
            site_id: The UniFi site UUID.
            default_ip: Global fallback IP.
            existing_policies: Pre-fetched name → policy map.

        Returns:
            None
        """
        local_name = to_local_policy_name(record_name)
        if local_name == record_name:
            return
        if cfg is None or not cfg.unifi_local_enabled:
            await self._remove_policy(local_name, site_id, existing_policies, label="local ")
            return
        local_target_ip = (
            cfg.unifi_local_static_ip.strip()
            or cfg.unifi_static_ip.strip()
            or default_ip.strip()
        )
        if not local_target_ip:
            self._log.log(f"UniFi: skipped local policy '{local_name}' — no IP configured.", level="WARNING")
            self._failed += 1
            return
        if await self._ensure_policy(local_name, site_id, local_target_ip, existing_policies, label="local "):
            # NOTE: Stamp last_checked for .local-only records so the dashboard
            # shows a timestamp even when the parent policy is disabled.
            self._stats.record_check(record_name)

    async def _ensure_policy(
        self,
        name: str,
        site_id: str,
        target_ip: str,
        existing_policies: dict[str, DnsRecord],
        *,
        label: str,
    ) -> bool:
        """
        Creates or updates a policy so it points at target_ip.

        Args:
            name: The DNS name of the policy.
            site_id: The UniFi site UUID.
            target_ip: The desired IP.
            existing_policies: Pre-fetched name → policy map.
            label: Message label, "" for the main policy or "local " for the companion.

        Returns:
            True if the policy is in sync (created, updated, or already matching),
            False if the operation failed.
        """
        try:
            existing = existing_policies.get(name)
            if existing is None:
                await self._client.create_record(site_id, name, target_ip)
                self._log.log(f"UniFi: created {label}policy '{name}' → {target_ip} ✓", level="INFO")
                self._created += 1
            elif existing.content != target_ip:
                await self._client.update_record(site_id, existing, target_ip)
                self._log.log(f"UniFi: updated {label}policy '{name}' → {target_ip} ✓", level="INFO")
                self._updated += 1
            else:
                logger.debug("UniFi policy '%s' already up to date (%s).", name, target_ip)
                self._log.log(f"UniFi: {label}'{name}' already in sync ({target_ip}).", level="INFO")
                self._unchanged += 1
            return True
        except UnifiProviderError as exc:
            self._log.log(f"UniFi: failed to sync {label}policy '{name}' — {exc}", level="ERROR")
            logger.error("UniFi sync failed for %s: %s", name, exc)
            self._failed += 1
            return False

    async def _remove_policy(
        self,
        name: str,
        site_id: str,
        existing_policies: dict[str, DnsRecord],
        *,
        label: str,
    ) -> None:
        """
        Deletes a policy if one exists (no-op otherwise).

        Args:
            name: The DNS name of the policy.
            site_id: The UniFi site UUID.
            existing_policies: Pre-fetched name → policy map.
            label: Message label, "" for the main policy or "local " for the companion.

        Returns:
            None
        """
        try:
            existing = existing_policies.get(name)
            if existing is not None:
                await self._client.delete_record(site_id, existing.id)
                self._log.log(f"UniFi: removed {label}policy '{name}' (disabled by user).", level="INFO")
                self._deleted += 1
        except UnifiProviderError as exc:
            self._log.log(f"UniFi: failed to remove {label}policy '{name}' — {exc}", level="ERROR")
            logger.error("UniFi %spolicy removal failed for %s: %s", label, name, exc)
            self._failed += 1
