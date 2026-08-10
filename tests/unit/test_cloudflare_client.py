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


@pytest.mark.asyncio
async def test_list_records_paginates_across_pages(mock_http):
    """list_records must follow result_info.total_pages to fetch every A-record."""
    page1 = {
        "success": True,
        "result": [_record_dict(id="r1", name="a.example.com")],
        "result_info": {"page": 1, "per_page": 100, "count": 1, "total_count": 2, "total_pages": 2},
        "errors": [],
    }
    page2 = {
        "success": True,
        "result": [_record_dict(id="r2", name="b.example.com")],
        "result_info": {"page": 2, "per_page": 100, "count": 1, "total_count": 2, "total_pages": 2},
        "errors": [],
    }
    route = mock_http.get(f"{_BASE}/zones/{_ZONE}/dns_records").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        result = await cf.list_records(_ZONE)

    assert len(result) == 2
    assert {r.id for r in result} == {"r1", "r2"}
    pages = [c.request.url.params.get("page") for c in route.calls]
    assert pages == ["1", "2"]


@pytest.mark.asyncio
async def test_list_records_single_page_when_result_info_missing(mock_http):
    """list_records must not loop when the API omits result_info (single page)."""
    records = [_record_dict(id="r1", name="a.example.com"), _record_dict(id="r2", name="b.example.com")]
    route = mock_http.get(f"{_BASE}/zones/{_ZONE}/dns_records").mock(
        return_value=httpx.Response(200, json=_cf_response(records))
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        result = await cf.list_records(_ZONE)

    assert len(result) == 2
    assert len(route.calls) == 1


@pytest.mark.asyncio
async def test_list_records_retries_on_transient_5xx(mock_http):
    """A transient 5xx from list_records must be retried before succeeding."""
    records = [_record_dict(id="r1", name="a.example.com")]
    route = mock_http.get(f"{_BASE}/zones/{_ZONE}/dns_records").mock(
        side_effect=[
            httpx.Response(500, json={"success": False, "errors": [{"message": "boom"}], "result": []}),
            httpx.Response(200, json=_cf_response(records)),
        ]
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        result = await cf.list_records(_ZONE)

    assert len(result) == 1
    assert len(route.calls) == 2


@pytest.mark.asyncio
async def test_list_records_does_not_retry_on_4xx(mock_http):
    """A 4xx response must fail immediately without retries."""
    route = mock_http.get(f"{_BASE}/zones/{_ZONE}/dns_records").mock(
        return_value=httpx.Response(403, json={"errors": ["forbidden"]})
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        with pytest.raises(DnsProviderError):
            await cf.list_records(_ZONE)
    assert len(route.calls) == 1


# ---------------------------------------------------------------------------
# list_records — shared listing cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_records_served_from_cache(mock_http):
    """A cached zone listing must skip the network call on repeat reads."""
    records = [_record_dict(id="r1", name="a.example.com")]
    route = mock_http.get(f"{_BASE}/zones/{_ZONE}/dns_records").mock(
        return_value=httpx.Response(200, json=_cf_response(records))
    )
    cache: dict = {}
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN, cache=cache)
        first = await cf.list_records(_ZONE)
        second = await cf.list_records(_ZONE)

    assert len(first) == 1 and len(second) == 1
    assert len(route.calls) == 1
    assert f"list_records:{_ZONE}" in cache


@pytest.mark.asyncio
async def test_update_record_invalidates_zone_listing(mock_http):
    """A mutation must drop the cached zone listing so the next read re-fetches."""
    existing = DnsRecord(id="rec1", name="home.example.com", content="1.1.1.1",
                         type="A", ttl=1, proxied=False, zone_id=_ZONE)
    records = [_record_dict(id="rec1", name="home.example.com", content="9.9.9.9")]
    mock_http.put(f"{_BASE}/zones/{_ZONE}/dns_records/rec1").mock(
        return_value=httpx.Response(200, json=_cf_response(records[0]))
    )
    list_route = mock_http.get(f"{_BASE}/zones/{_ZONE}/dns_records").mock(
        return_value=httpx.Response(200, json=_cf_response(records))
    )
    cache: dict = {}
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN, cache=cache)
        await cf.list_records(_ZONE)          # populates cache
        await cf.update_record(_ZONE, existing, "9.9.9.9")  # invalidates
        await cf.list_records(_ZONE)          # must re-fetch

    assert len(list_route.calls) == 2
    assert f"list_records:{_ZONE}" in cache


# ---------------------------------------------------------------------------
# create_record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_record_posts_correct_payload(mock_http):
    """create_record must POST an A-record payload and return the created DnsRecord."""
    import json as _json

    new_record = _record_dict(id="new-rec-1", name="new.example.com", content="5.6.7.8")
    route = mock_http.post(f"{_BASE}/zones/{_ZONE}/dns_records").mock(
        return_value=httpx.Response(200, json=_cf_response(new_record))
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        result = await cf.create_record(_ZONE, "new.example.com", "5.6.7.8")

    assert isinstance(result, DnsRecord)
    assert result.id == "new-rec-1"
    assert result.name == "new.example.com"
    assert result.content == "5.6.7.8"

    body = _json.loads(route.calls[0].request.content)
    assert body["type"] == "A"
    assert body["name"] == "new.example.com"
    assert body["content"] == "5.6.7.8"
    assert body["ttl"] == 1
    assert body["proxied"] is False


@pytest.mark.asyncio
async def test_create_record_raises_on_http_error(mock_http):
    """create_record raises DnsProviderError when the API returns a 4xx/5xx status."""
    mock_http.post(f"{_BASE}/zones/{_ZONE}/dns_records").mock(
        return_value=httpx.Response(422, json={"errors": ["invalid record"]})
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        with pytest.raises(DnsProviderError):
            await cf.create_record(_ZONE, "new.example.com", "5.6.7.8")


@pytest.mark.asyncio
async def test_create_record_raises_on_success_false(mock_http):
    """create_record raises DnsProviderError when success=false in the response body."""
    mock_http.post(f"{_BASE}/zones/{_ZONE}/dns_records").mock(
        return_value=httpx.Response(
            200,
            json={"success": False, "errors": [{"message": "record already exists"}], "result": {}},
        )
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        with pytest.raises(DnsProviderError, match="success=false"):
            await cf.create_record(_ZONE, "dup.example.com", "1.2.3.4")


# ---------------------------------------------------------------------------
# delete_record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_record_sends_delete_request(mock_http):
    """delete_record must call DELETE on the record endpoint without raising."""
    mock_http.delete(f"{_BASE}/zones/{_ZONE}/dns_records/rec1").mock(
        return_value=httpx.Response(200, json=_cf_response({"id": "rec1"}))
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        # Must not raise
        await cf.delete_record(_ZONE, "rec1")


@pytest.mark.asyncio
async def test_delete_record_raises_on_http_error(mock_http):
    """delete_record raises DnsProviderError when the API returns 404."""
    mock_http.delete(f"{_BASE}/zones/{_ZONE}/dns_records/missing-id").mock(
        return_value=httpx.Response(404, json={"errors": [{"message": "record not found"}]})
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        with pytest.raises(DnsProviderError):
            await cf.delete_record(_ZONE, "missing-id")


@pytest.mark.asyncio
async def test_delete_record_raises_on_network_error(mock_http):
    """delete_record raises DnsProviderError on a network-level failure."""
    mock_http.delete(f"{_BASE}/zones/{_ZONE}/dns_records/rec1").mock(
        side_effect=httpx.ConnectError("connection reset")
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        with pytest.raises(DnsProviderError, match="connection reset"):
            await cf.delete_record(_ZONE, "rec1")


# ---------------------------------------------------------------------------
# verify_token / list_zones (Cloudflare-specific helpers)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_token_returns_true_when_active(mock_http):
    """verify_token returns True when the token status is active."""
    mock_http.get(f"{_BASE}/user/tokens/verify").mock(
        return_value=httpx.Response(200, json=_cf_response({"id": "tok", "status": "active"}))
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        assert await cf.verify_token() is True


@pytest.mark.asyncio
async def test_verify_token_returns_false_when_inactive(mock_http):
    """verify_token returns False when the token is not active."""
    mock_http.get(f"{_BASE}/user/tokens/verify").mock(
        return_value=httpx.Response(200, json=_cf_response({"id": "tok", "status": "disabled"}))
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        assert await cf.verify_token() is False


@pytest.mark.asyncio
async def test_list_zones_returns_zones(mock_http):
    """list_zones returns name/id dicts for every zone."""
    mock_http.get(f"{_BASE}/zones").mock(
        return_value=httpx.Response(200, json=_cf_response([
            {"name": "example.com", "id": "zone1"},
            {"name": "other.net", "id": "zone2"},
        ]))
    )
    async with httpx.AsyncClient() as client:
        cf = CloudflareClient(client, _TOKEN)
        zones = await cf.list_zones()
    assert zones == [
        {"name": "example.com", "id": "zone1"},
        {"name": "other.net", "id": "zone2"},
    ]
