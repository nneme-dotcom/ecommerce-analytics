import logging

import httpx
import pytest

from ingestion.mercadolibre.client import MercadoLibreClient, RequestCapReached

ACCESS = "fake-access-xyz"
BASE = "https://api.test"


class Recorder:
    """Records sleeps instead of sleeping."""

    def __init__(self):
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def make_client(handler, **kwargs):
    sleep = kwargs.pop("sleep", Recorder())
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return MercadoLibreClient(ACCESS, base_url=BASE, http=http, sleep=sleep, **kwargs), sleep


def test_bearer_header_and_no_token_in_url():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client, _ = make_client(handler)
    assert client.get("/trends/MLA", params={"limit": 5}) == {"ok": True}

    request = seen[0]
    assert request.headers["authorization"] == f"Bearer {ACCESS}"
    assert ACCESS not in str(request.url)
    assert "access_token" not in str(request.url)
    assert ACCESS not in repr(client)


def test_retry_on_429_then_success_with_exponential_backoff():
    statuses = iter([429, 429, 200])

    def handler(request):
        status = next(statuses)
        return httpx.Response(status, json={"n": 1} if status == 200 else {"error": "slow down"})

    client, sleep = make_client(handler, delay_s=0.5, backoff_base_s=1.0)
    assert client.get("/products/MLA1") == {"n": 1}
    assert client.requests_made == 3
    assert client.errors == []
    # delay 0.5 before every request but the first, plus backoff 1s then 2s
    assert sleep.sleeps == [1.0, 0.5, 2.0, 0.5]


def test_retry_after_header_is_respected():
    statuses = iter([429, 200])

    def handler(request):
        status = next(statuses)
        headers = {"Retry-After": "7"} if status == 429 else {}
        return httpx.Response(status, json={}, headers=headers)

    client, sleep = make_client(handler, delay_s=0)
    client.get("/x")
    assert sleep.sleeps == [7.0]


def test_gives_up_after_max_retries_and_records_error_without_payload():
    def handler(request):
        return httpx.Response(503, json={"secret_payload": "do-not-store"})

    client, _ = make_client(handler, max_retries=3)
    assert client.get("/products/MLA1/items", params={"offset": 0}) is None
    assert client.requests_made == 4  # 1 try + 3 retries
    assert client.errors == [{"path": "/products/MLA1/items", "status": 503}]
    assert client.error_summary() == [{"path": "/products/MLA1/items", "status": 503, "count": 1}]


def test_4xx_is_not_retried():
    client, _ = make_client(lambda r: httpx.Response(403, json={}))
    assert client.get("/items/MLA1") is None
    assert client.requests_made == 1
    assert client.errors == [{"path": "/items/MLA1", "status": 403}]


def test_transport_error_is_retried_and_recorded_with_null_status():
    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)

    client, _ = make_client(handler, max_retries=2)
    assert client.get("/trends/MLA") is None
    assert client.requests_made == 3
    assert client.errors == [{"path": "/trends/MLA", "status": None}]


def test_request_cap_enforced():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    client, _ = make_client(handler, max_requests=3)
    for _ in range(3):
        client.get("/x")
    with pytest.raises(RequestCapReached):
        client.get("/x")
    assert len(calls) == 3
    assert client.requests_made == 3


def test_request_cap_counts_retries():
    client, _ = make_client(lambda r: httpx.Response(500), max_requests=2, max_retries=3)
    with pytest.raises(RequestCapReached):
        client.get("/x")
    assert client.requests_made == 2


def test_no_token_in_any_log_record(caplog):
    caplog.set_level(logging.DEBUG)
    statuses = iter([500, 200, 404])

    def handler(request):
        return httpx.Response(next(statuses), json={})

    client, _ = make_client(handler, max_requests=3)
    client.get("/a")
    client.get("/b")
    with pytest.raises(RequestCapReached):
        client.get("/c")

    assert caplog.records
    for record in caplog.records:
        assert ACCESS not in record.getMessage()
        assert "Bearer" not in record.getMessage()


def test_any_httpx_error_is_retried_and_recorded():
    def handler(request):
        raise httpx.RemoteProtocolError("peer closed", request=request)

    client, _ = make_client(handler, max_retries=1)
    assert client.get("/trends/MLA") is None
    assert client.requests_made == 2
    assert client.errors == [{"path": "/trends/MLA", "status": None}]


@pytest.mark.parametrize("body", [b"null", b"not json"])
def test_200_with_null_or_invalid_body_is_recorded(body):
    client, _ = make_client(lambda r: httpx.Response(200, content=body))
    assert client.get("/products/MLA1") is None
    assert client.errors == [{"path": "/products/MLA1", "status": 200}]


def test_not_found_ok_returns_none_without_recording():
    client, _ = make_client(lambda r: httpx.Response(404, json={}))
    assert client.get("/products/MLA1/items", not_found_ok=True) is None
    assert client.errors == []
    assert client.get("/products/MLA1/items") is None
    assert client.errors == [{"path": "/products/MLA1/items", "status": 404}]


def test_record_error_is_public_and_aggregated():
    client, _ = make_client(lambda r: httpx.Response(200, json={}))
    client.record_error("/products/X", None)
    client.record_error("/products/X", None)
    assert client.error_summary() == [{"path": "/products/X", "status": None, "count": 2}]


def test_empty_token_rejected():
    with pytest.raises(ValueError):
        MercadoLibreClient("")
