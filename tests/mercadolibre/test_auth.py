import logging
from urllib.parse import parse_qs

import httpx
import pytest

from ingestion.mercadolibre.auth import (
    TOKEN_URL,
    AuthError,
    ReauthorizationRequired,
    TransientAuthError,
    exchange_authorization_code,
    refresh_access_token,
)

# Obviously fake values; real tokens never appear in tests.
OLD_REFRESH = "fake-refresh-old"
NEW_REFRESH = "fake-refresh-new"
ACCESS = "fake-access-xyz"
SECRET = "fake-client-secret"


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_refresh_success_returns_rotated_refresh_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers["content-type"]
        seen["form"] = parse_qs(request.content.decode())
        return httpx.Response(
            200,
            json={
                "access_token": ACCESS,
                "token_type": "Bearer",
                "expires_in": 21600,
                "scope": "offline_access read",
                "user_id": 123,
                "refresh_token": NEW_REFRESH,
            },
        )

    with _client(handler) as http:
        tokens = refresh_access_token("app-id", SECRET, OLD_REFRESH, http=http)

    assert tokens.access_token == ACCESS
    assert tokens.refresh_token == NEW_REFRESH
    assert tokens.refresh_token != OLD_REFRESH
    assert tokens.expires_in == 21600
    assert seen["url"] == TOKEN_URL  # nothing in the query string
    assert seen["content_type"] == "application/x-www-form-urlencoded"
    assert seen["form"] == {
        "grant_type": ["refresh_token"],
        "client_id": ["app-id"],
        "client_secret": [SECRET],
        "refresh_token": [OLD_REFRESH],
    }
    assert ACCESS not in repr(tokens) and NEW_REFRESH not in repr(tokens)


@pytest.mark.parametrize("status", [400, 401])
def test_invalid_grant_raises_reauthorization_required(status):
    def handler(request):
        return httpx.Response(
            status, json={"error": "invalid_grant", "error_description": "invalid refresh_token"}
        )

    with _client(handler) as http, pytest.raises(ReauthorizationRequired) as excinfo:
        refresh_access_token("app-id", SECRET, OLD_REFRESH, http=http)

    message = str(excinfo.value)
    assert "re-authorize" in message
    assert "python -m ingestion.mercadolibre.authorize" in message
    assert OLD_REFRESH not in message and SECRET not in message
    assert excinfo.value.status == status


def test_non_transient_error_raises_auth_error_without_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, json={"error": "forbidden"})

    with _client(handler) as http, pytest.raises(AuthError) as excinfo:
        refresh_access_token("app-id", SECRET, OLD_REFRESH, http=http, sleep=lambda s: None)
    assert not isinstance(excinfo.value, (ReauthorizationRequired, TransientAuthError))
    assert excinfo.value.status == 403
    assert len(calls) == 1


def test_missing_refresh_token_in_response_is_an_error():
    with _client(lambda r: httpx.Response(200, json={"access_token": ACCESS})) as http:
        with pytest.raises(AuthError):
            refresh_access_token("app-id", SECRET, OLD_REFRESH, http=http)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_status_is_retried_then_succeeds(status):
    responses = [
        httpx.Response(status),
        httpx.Response(status),
        httpx.Response(200, json={"access_token": ACCESS, "refresh_token": NEW_REFRESH}),
    ]
    sleeps = []
    with _client(lambda r: responses.pop(0)) as http:
        tokens = refresh_access_token(
            "app-id", SECRET, OLD_REFRESH, http=http, backoff_base_s=1.0, sleep=sleeps.append
        )
    assert tokens.refresh_token == NEW_REFRESH
    assert sleeps == [1.0, 2.0]


def test_transient_failure_after_retries_raises_transient_auth_error():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(502, text="bad gateway")

    with _client(handler) as http, pytest.raises(TransientAuthError) as excinfo:
        refresh_access_token("app-id", SECRET, OLD_REFRESH, http=http, sleep=lambda s: None)
    assert excinfo.value.status == 502
    assert len(calls) == 4  # 1 try + 3 retries


@pytest.mark.parametrize(
    "exc", [httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError]
)
def test_network_errors_are_retried_and_wrapped(exc):
    calls = []

    def handler(request):
        calls.append(request)
        raise exc("network down", request=request)

    with _client(handler) as http, pytest.raises(TransientAuthError) as excinfo:
        refresh_access_token("app-id", SECRET, OLD_REFRESH, http=http, sleep=lambda s: None)
    assert excinfo.value.status is None
    assert len(calls) == 4


def test_invalid_grant_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"error": "invalid_grant"})

    with _client(handler) as http, pytest.raises(ReauthorizationRequired):
        refresh_access_token("app-id", SECRET, OLD_REFRESH, http=http, sleep=lambda s: None)
    assert len(calls) == 1


def test_authorization_code_exchange_sends_pkce_verifier():
    seen = {}

    def handler(request):
        seen["form"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"access_token": ACCESS, "refresh_token": NEW_REFRESH})

    with _client(handler) as http:
        tokens = exchange_authorization_code(
            "app-id", SECRET, "fake-code", "https://www.google.com", "fake-verifier", http=http
        )

    assert tokens.refresh_token == NEW_REFRESH
    assert seen["form"]["grant_type"] == ["authorization_code"]
    assert seen["form"]["code_verifier"] == ["fake-verifier"]
    assert seen["form"]["redirect_uri"] == ["https://www.google.com"]


def test_no_token_or_secret_in_logs(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request):
        return httpx.Response(200, json={"access_token": ACCESS, "refresh_token": NEW_REFRESH})

    with _client(handler) as http:
        refresh_access_token("app-id", SECRET, OLD_REFRESH, http=http)
    with _client(lambda r: httpx.Response(400, json={"error": "invalid_grant"})) as http:
        with pytest.raises(ReauthorizationRequired):
            refresh_access_token("app-id", SECRET, OLD_REFRESH, http=http)

    assert caplog.records, "expected some log output"
    for record in caplog.records:
        text = record.getMessage()
        for value in (ACCESS, OLD_REFRESH, NEW_REFRESH, SECRET):
            assert value not in text
