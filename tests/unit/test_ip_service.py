"""
tests/unit/test_ip_service.py

Unit tests for services/ip_service.py.
Verifies happy-path IP fetching and typed error raising on failure.
"""

from __future__ import annotations

import pytest
import httpx
import respx

from services.ip_service import IpService
from exceptions import IpFetchError


@pytest.mark.asyncio
async def test_get_public_ip_returns_ip(mock_http):
    """IpService must return the plain-text IP from the upstream provider."""
    mock_http.get("https://api.ipify.org").mock(
        return_value=httpx.Response(200, text="1.2.3.4")
    )
    async with httpx.AsyncClient() as client:
        service = IpService(http_client=client)
        ip = await service.get_public_ip()
    assert ip == "1.2.3.4"


@pytest.mark.asyncio
async def test_get_public_ip_strips_whitespace(mock_http):
    """IpService must strip leading/trailing whitespace from the response."""
    mock_http.get("https://api.ipify.org").mock(
        return_value=httpx.Response(200, text="  1.2.3.4\n")
    )
    async with httpx.AsyncClient() as client:
        service = IpService(http_client=client)
        ip = await service.get_public_ip()
    assert ip == "1.2.3.4"


@pytest.mark.asyncio
async def test_get_public_ip_raises_on_network_error(mock_http):
    """IpService must raise IpFetchError when the upstream is unreachable."""
    mock_http.get("https://api.ipify.org").mock(
        side_effect=httpx.ConnectError("timeout")
    )
    async with httpx.AsyncClient() as client:
        service = IpService(http_client=client)
        with pytest.raises(IpFetchError):
            await service.get_public_ip()


@pytest.mark.asyncio
async def test_get_public_ip_raises_on_http_error(mock_http):
    """IpService must raise IpFetchError when the upstream returns a non-200 status."""
    mock_http.get("https://api.ipify.org").mock(
        return_value=httpx.Response(503)
    )
    async with httpx.AsyncClient() as client:
        service = IpService(http_client=client)
        with pytest.raises(IpFetchError):
            await service.get_public_ip()
