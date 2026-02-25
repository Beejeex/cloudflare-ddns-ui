"""
services/kubernetes_service.py

Responsibility: Discovers DNS hostnames by reading Kubernetes Ingress resources
across all namespaces. Connection is auto-detected: in-cluster service account
first, then /config/kubeconfig as a fallback.
Does NOT: update DNS records, interact with Cloudflare, or manage cluster state.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from exceptions import KubernetesError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngressRecord:
    """
    Represents a single hostname discovered from a Kubernetes Ingress resource.

    Attributes:
        hostname: The DNS hostname defined in the Ingress rule (e.g. "app.example.com").
        namespace: Kubernetes namespace the Ingress belongs to.
        ingress_name: Name of the Ingress resource.
    """

    hostname: str
    namespace: str
    ingress_name: str


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class KubernetesService:
    """
    Discovers DNS hostnames from Kubernetes Ingress resources.

    Connection is auto-detected on first use: the service account mounted
    inside the container is tried first (in-cluster), then
    /config/kubeconfig as a file-based fallback. Discovery is skipped
    entirely when the feature is disabled via the Settings toggle.

    Kubernetes API calls are blocking and are offloaded to a thread
    via asyncio.to_thread to keep the async event loop unblocked.

    Collaborators:
        - kubernetes Python client: reads NetworkingV1 Ingress resources
    """

    # Well-known path for the file-based fallback — matches the /config volume mount.
    _KUBECONFIG_FALLBACK = "/config/kubeconfig"

    def __init__(self, enabled: bool) -> None:
        """
        Initialises the service.

        Args:
            enabled: When False, list_ingress_records() returns an empty list
                immediately without contacting the cluster.
        """
        self._enabled = enabled

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    async def list_ingress_records(self) -> list[IngressRecord]:
        """
        Returns all hostnames discovered from Ingress resources in all namespaces.

        Returns an empty list immediately when the feature is disabled.
        Offloads the blocking Kubernetes API call to a thread pool worker.

        Returns:
            A list of IngressRecord instances, one per hostname.

        Raises:
            KubernetesError: If the cluster is unreachable, auth fails, or no
                kubeconfig can be found.
        """
        if not self._enabled:
            return []
        try:
            return await asyncio.to_thread(self._collect_ingress_records)
        except KubernetesError:
            raise
        except Exception as exc:
            raise KubernetesError(f"Failed to connect to Kubernetes cluster: {exc}") from exc

    def is_enabled(self) -> bool:
        """
        Returns True if Kubernetes Ingress discovery is enabled.

        Returns:
            True if enabled, False otherwise.
        """
        return self._enabled

    # ---------------------------------------------------------------------------
    # Internal helpers (sync — run via asyncio.to_thread)
    # ---------------------------------------------------------------------------

    def _collect_ingress_records(self) -> list[IngressRecord]:
        """
        Synchronous implementation of Ingress discovery.

        Attempts in-cluster service-account auth first, then falls back to
        /config/kubeconfig. Initialises the NetworkingV1 API client and
        iterates over all Ingress resources to extract hostnames.

        Returns:
            A list of IngressRecord instances.

        Raises:
            KubernetesError: If the API call fails or credentials are invalid.
        """
        # NOTE: Import inside the method so that the kubernetes package is only
        # required at discovery time — the rest of the app starts fine without it.
        try:
            from kubernetes import client as k8s_client
            from kubernetes import config as k8s_config
            from kubernetes.client.exceptions import ApiException
        except ImportError as exc:
            raise KubernetesError(
                "The 'kubernetes' package is not installed. "
                "Re-build the container image to include it."
            ) from exc

        # NOTE: Try the in-cluster service account first (standard Kubernetes
        # deployment). If that fails (e.g. running locally), fall back to the
        # well-known file path at the /config volume mount.
        try:
            k8s_config.load_incluster_config()
            logger.debug("Kubernetes: using in-cluster service account.")
        except Exception:
            # NOTE: Broad catch is intentional — load_incluster_config raises
            # ConfigException when not running inside a pod, but we want to
            # fall through to the file fallback regardless of error type.
            try:
                k8s_config.load_kube_config(config_file=self._KUBECONFIG_FALLBACK)
                logger.debug("Kubernetes: using kubeconfig at %s.", self._KUBECONFIG_FALLBACK)
            except Exception as exc:
                raise KubernetesError(
                    f"Could not load cluster credentials: no in-cluster SA and "
                    f"no kubeconfig at '{self._KUBECONFIG_FALLBACK}': {exc}"
                ) from exc

        try:
            api = k8s_client.NetworkingV1Api()
            ingress_list = api.list_ingress_for_all_namespaces()
        except ApiException as exc:
            raise KubernetesError(
                f"Kubernetes API error {exc.status}: {exc.reason}"
            ) from exc

        records: list[IngressRecord] = []
        for ingress in ingress_list.items:
            namespace: str = ingress.metadata.namespace or "default"
            name: str = ingress.metadata.name or ""

            if not ingress.spec or not ingress.spec.rules:
                continue

            for rule in ingress.spec.rules:
                # Only rules with an explicit host become DNS records
                if rule.host:
                    records.append(
                        IngressRecord(
                            hostname=rule.host,
                            namespace=namespace,
                            ingress_name=name,
                        )
                    )

        logger.info(
            "Kubernetes ingress discovery: found %d hostname(s) across %d ingress resource(s).",
            len(records),
            len(ingress_list.items),
        )
        return records
