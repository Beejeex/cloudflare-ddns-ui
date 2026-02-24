"""
tests/unit/test_cloudflare_client.py

Unit tests for cloudflare/cloudflare_client.py.
All Cloudflare API calls are intercepted by respx — no real network traffic.
"""

from __future__ import annotations

import json
import pytest
import httpx
import respx

from cloudflare.cloudflare_client import CloudflareClient
from cloudflare.dns_provider import DnsRecord
from exceptions import DnsProviderError

_ZONE = "zone123"
_TOKEN = "test-token"
_BASE = "https://api.cloudflare.com/client/v4"


def _cf_response(result, success=True):
    """Helper: build a Cloudflare-shaped JSON response dict."""
    return {"success": success, "result": result, "errors": []}


def _record_dict(**kwargs):
    return {
        "id": kwargs.get("id", "rec1"),
        "name": kwargs.get("name", "home.example.com"),
        "content": kwargs.get("content", "1.2.3.4"),
        "type": "A",
        "ttl": 1,
        "proxied": False,
        "zone_id": _ZONE,
    }


# ---------------------------------------------------------------------------
# get_record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_record_returns_record(mock_http):
    """get_record returns a DnsRecord when the API returns one result."""
    mock_http.get(f"{_BASE}/zones/{_ZONE}/dns_records").mock(
        return_value=httpx.Response(200, json=_cf_response([_record_dict()]))
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        record = await cf.get_record(_ZONE, "home.example.com")

    assert isinstance(record, DnsRecord)
    assert record.content == "1.2.3.4"
    assert record.name == "home.example.com"


@pytest.mark.asyncio
async def test_get_record_returns_none_when_not_found(mock_http):
    """get_record returns None when the API result list is empty."""
    mock_http.get(f"{_BASE}/zones/{_ZONE}/dns_records").mock(
        return_value=httpx.Response(200, json=_cf_response([]))
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        record = await cf.get_record(_ZONE, "missing.example.com")

    assert record is None


@pytest.mark.asyncio
async def test_get_record_raises_on_api_failure(mock_http):
    """get_record raises DnsProviderError when success=false."""
    mock_http.get(f"{_BASE}/zones/{_ZONE}/dns_records").mock(
        return_value=httpx.Response(200, json={"success": False, "errors": [{"message": "bad token"}], "result": []})
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        with pytest.raises(DnsProviderError):
            await cf.get_record(_ZONE, "home.example.com")


@pytest.mark.asyncio
async def test_get_record_raises_on_http_error(mock_http):
    """get_record raises DnsProviderError on HTTP 401."""
    mock_http.get(f"{_BASE}/zones/{_ZONE}/dns_records").mock(
        return_value=httpx.Response(401, json={"errors": ["unauthorized"]})
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        with pytest.raises(DnsProviderError):
            await cf.get_record(_ZONE, "home.example.com")


# ---------------------------------------------------------------------------
# update_record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_record_returns_updated_record(mock_http):
    """update_record returns the updated DnsRecord with the new IP."""
    existing = DnsRecord(id="rec1", name="home.example.com", content="1.1.1.1",
                         type="A", ttl=1, proxied=False, zone_id=_ZONE)
    updated_dict = _record_dict(content="9.9.9.9")

    mock_http.put(f"{_BASE}/zones/{_ZONE}/dns_records/rec1").mock(
        return_value=httpx.Response(200, json=_cf_response(updated_dict))
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        result = await cf.update_record(_ZONE, existing, "9.9.9.9")

    assert result.content == "9.9.9.9"


# ---------------------------------------------------------------------------
# list_records
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_records_returns_all_records(mock_http):
    """list_records returns all A-records as DnsRecord instances."""
    records = [_record_dict(id="r1", name="a.example.com"), _record_dict(id="r2", name="b.example.com")]
    mock_http.get(f"{_BASE}/zones/{_ZONE}/dns_records").mock(
        return_value=httpx.Response(200, json=_cf_response(records))
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        result = await cf.list_records(_ZONE)

    assert len(result) == 2
    assert all(isinstance(r, DnsRecord) for r in result)
