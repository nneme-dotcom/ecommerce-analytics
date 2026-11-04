from decimal import Decimal

import httpx
import pytest

from ingestion.enrichment.http import FetchError, JsonClient


class Sleeps(list):
    def __call__(self, seconds):
        self.append(seconds)


def make(handler, **kwargs):
    sleeps = Sleeps()
    client = JsonClient(
        "https://api.test/v1",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps,
        **kwargs,
    )
    return client, sleeps


def test_parses_floats_as_decimal_and_delays_between_calls():
    def handler(request):
        return httpx.Response(200, content=b'{"rate": 5.9123456789012345, "n": 2}')

    client, sleeps = make(handler, delay_s=0.5)
    assert client.get_json("/a") == (200, {"rate": Decimal("5.9123456789012345"), "n": 2})
    client.get_json("/b")
    assert sleeps == [0.5]
    assert client.requests_made == 2


def test_retries_429_and_5xx_with_backoff_and_retry_after():
    responses = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(502),
        httpx.Response(200, json=[]),
    ]
    client, sleeps = make(lambda r: responses.pop(0), delay_s=0.5, backoff_base_s=1.0)
    assert client.get_json("/x") == (200, [])
    # 7 s (Retry-After), delay, 2 s backoff, delay
    assert sleeps == [7.0, 0.5, 2.0, 0.5]
    assert client.requests_made == 3


def test_network_errors_exhaust_retries():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    client, _ = make(handler, max_retries=2)
    with pytest.raises(FetchError, match="network error \\(ReadTimeout\\) after 3 attempts"):
        client.get_json("/x")


def test_client_errors_fail_fast_unless_allowed():
    client, _ = make(lambda r: httpx.Response(404))
    assert client.get_json("/x", ok_statuses=(404,)) == (404, None)
    with pytest.raises(FetchError, match="HTTP 404"):
        client.get_json("/x")
    assert client.requests_made == 2


def test_request_cap_counts_retries():
    client, _ = make(lambda r: httpx.Response(503), max_requests=2, max_retries=5)
    with pytest.raises(FetchError, match="request cap reached"):
        client.get_json("/x")
    assert client.requests_made == 2


def test_non_json_body_is_an_error():
    client, _ = make(lambda r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(FetchError, match="not JSON"):
        client.get_json("/x")
