import logging
from urllib.parse import parse_qs

import httpx
import pytest

from ingestion.mercadolibre.auth import (
    TOKEN_URL,
    AuthError,
    ReauthorizationRequired,
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


def test_other_error_raises_auth_error_not_reauth():
    with _client(lambda r: httpx.Response(500, text="oops")) as http:
        with pytest.raises(AuthError) as excinfo:
            refresh_access_token("app-id", SECRET, OLD_REFRESH, http=http)
    assert not isinstance(excinfo.value, ReauthorizationRequired)
    assert excinfo.value.status == 500


def test_missing_refresh_token_in_response_is_an_error():
    with _client(lambda r: httpx.Response(200, json={"access_token": ACCESS})) as http:
        with pytest.raises(AuthError):
            refresh_access_token("app-id", SECRET, OLD_REFRESH, http=http)


def test_transport_error_is_wrapped():
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    with _client(handler) as http, pytest.raises(AuthError):
        refresh_access_token("app-id", SECRET, OLD_REFRESH, http=http)


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
