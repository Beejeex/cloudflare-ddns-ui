"""
tests/unit/test_kubernetes_service.py

Unit tests for KubernetesService.
All Kubernetes API calls are mocked — no real cluster is required.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from exceptions import KubernetesError
from services.kubernetes_service import IngressRecord, KubernetesService


# ---------------------------------------------------------------------------
# Helpers — build fake Kubernetes Ingress objects
# ---------------------------------------------------------------------------


def _make_ingress(name: str, namespace: str, hosts: list[str]) -> SimpleNamespace:
    """Constructs a minimal fake Ingress object matching the kubernetes client shape."""
    rules = [SimpleNamespace(host=h) for h in hosts]
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace),
        spec=SimpleNamespace(rules=rules),
    )


def _make_ingress_list(items: list) -> SimpleNamespace:
    return SimpleNamespace(items=items)


# ---------------------------------------------------------------------------
# is_configured
# ---------------------------------------------------------------------------


def test_is_configured_returns_false_when_empty():
    svc = KubernetesService(kubeconfig_path="")
    assert svc.is_configured() is False


def test_is_configured_returns_false_for_whitespace():
    svc = KubernetesService(kubeconfig_path="   ")
    assert svc.is_configured() is False


def test_is_configured_returns_true_when_path_set():
    svc = KubernetesService(kubeconfig_path="/config/kubeconfig")
    assert svc.is_configured() is True


# ---------------------------------------------------------------------------
# list_ingress_records — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_ingress_records_returns_hostnames():
    """All hostnames from Ingress rules are returned as IngressRecord instances."""
    fake_list = _make_ingress_list([
        _make_ingress("app1", "default", ["app1.example.com"]),
        _make_ingress("app2", "prod", ["api.example.com", "www.example.com"]),
    ])

    with (
        patch("services.kubernetes_service.asyncio.to_thread") as mock_thread,
    ):
        svc = KubernetesService(kubeconfig_path="/config/kubeconfig")

        # Simulate what to_thread would return
        mock_thread.return_value = [
            IngressRecord("app1.example.com", "default", "app1"),
            IngressRecord("api.example.com", "prod", "app2"),
            IngressRecord("www.example.com", "prod", "app2"),
        ]

        records = await svc.list_ingress_records()

    assert len(records) == 3
    hostnames = [r.hostname for r in records]
    assert "app1.example.com" in hostnames
    assert "api.example.com" in hostnames
    assert "www.example.com" in hostnames


@pytest.mark.asyncio
async def test_list_ingress_records_empty_cluster():
    """An empty cluster returns an empty list without raising."""
    with patch("services.kubernetes_service.asyncio.to_thread") as mock_thread:
        mock_thread.return_value = []
        svc = KubernetesService(kubeconfig_path="/config/kubeconfig")
        records = await svc.list_ingress_records()

    assert records == []


# ---------------------------------------------------------------------------
# list_ingress_records — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_ingress_records_raises_kubernetes_error_on_api_failure():
    """A KubernetesError from the sync layer is propagated as-is."""
    with patch("services.kubernetes_service.asyncio.to_thread") as mock_thread:
        mock_thread.side_effect = KubernetesError("API 403: Forbidden")
        svc = KubernetesService(kubeconfig_path="/config/kubeconfig")

        with pytest.raises(KubernetesError, match="403"):
            await svc.list_ingress_records()


@pytest.mark.asyncio
async def test_list_ingress_records_wraps_unexpected_errors():
    """Unexpected exceptions from asyncio.to_thread are wrapped in KubernetesError."""
    with patch("services.kubernetes_service.asyncio.to_thread") as mock_thread:
        mock_thread.side_effect = OSError("network unreachable")
        svc = KubernetesService(kubeconfig_path="/config/kubeconfig")

        with pytest.raises(KubernetesError, match="network unreachable"):
            await svc.list_ingress_records()


# ---------------------------------------------------------------------------
# _collect_ingress_records (sync internals)
# ---------------------------------------------------------------------------


def test_collect_ingress_records_skips_rules_without_host():
    """Ingress rules that have no host value are silently skipped."""
    kubernetes = pytest.importorskip("kubernetes")

    fake_ingress = SimpleNamespace(
        metadata=SimpleNamespace(name="partial", namespace="default"),
        spec=SimpleNamespace(rules=[
            SimpleNamespace(host="valid.example.com"),
            SimpleNamespace(host=None),
            SimpleNamespace(host=""),
        ]),
    )
    fake_list = _make_ingress_list([fake_ingress])

    fake_api = MagicMock()
    fake_api.list_ingress_for_all_namespaces.return_value = fake_list

    with (
        patch("kubernetes.config.load_kube_config"),
        patch("kubernetes.client.NetworkingV1Api", return_value=fake_api),
    ):
        svc = KubernetesService(kubeconfig_path="/config/kubeconfig")
        records = svc._collect_ingress_records()

    assert len(records) == 1
    assert records[0].hostname == "valid.example.com"


def test_collect_ingress_records_skips_ingress_without_rules():
    """An Ingress with no rules is silently skipped."""
    kubernetes = pytest.importorskip("kubernetes")

    fake_ingress = SimpleNamespace(
        metadata=SimpleNamespace(name="no-rules", namespace="default"),
        spec=SimpleNamespace(rules=None),
    )
    fake_list = _make_ingress_list([fake_ingress])

    fake_api = MagicMock()
    fake_api.list_ingress_for_all_namespaces.return_value = fake_list

    with (
        patch("kubernetes.config.load_kube_config"),
        patch("kubernetes.client.NetworkingV1Api", return_value=fake_api),
    ):
        svc = KubernetesService(kubeconfig_path="/config/kubeconfig")
        records = svc._collect_ingress_records()

    assert records == []
