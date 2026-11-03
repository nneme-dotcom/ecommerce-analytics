import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ingestion.mercadolibre import authorize


def test_pkce_pair_is_s256_and_valid_length():
    verifier, challenge = authorize.make_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode()
    assert "=" not in verifier and "=" not in challenge
    assert authorize.make_pkce_pair()[0] != verifier  # random each time


def test_authorization_url_has_pkce_params():
    url = authorize.build_authorization_url("app-id", "https://www.google.com", "chal")
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == authorize.AUTHORIZATION_URL
    assert parse_qs(parsed.query) == {
        "response_type": ["code"],
        "client_id": ["app-id"],
        "redirect_uri": ["https://www.google.com"],
        "code_challenge": ["chal"],
        "code_challenge_method": ["S256"],
    }


@pytest.mark.parametrize(
    "pasted",
    [
        "TG-abc123",
        "  TG-abc123  ",
        "https://www.google.com/?code=TG-abc123",
        "https://www.google.com/?code=TG-abc123&state=x",
        "?code=TG-abc123",
        "code=TG-abc123",
    ],
)
def test_extract_code(pasted):
    assert authorize.extract_code(pasted) == "TG-abc123"


@pytest.mark.parametrize("pasted", ["", "https://www.google.com/?error=access_denied"])
def test_extract_code_rejects_bad_input(pasted):
    with pytest.raises(ValueError):
        authorize.extract_code(pasted)


def test_main_missing_env(monkeypatch, capsys):
    for name in authorize.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert authorize.main() == 2
    assert "ML_CLIENT_ID" in capsys.readouterr().err


def test_main_prints_refresh_token_once_and_never_access_token(monkeypatch, capsys):
    monkeypatch.setenv("ML_CLIENT_ID", "app-id")
    monkeypatch.setenv("ML_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("ML_REDIRECT_URI", "https://www.google.com")
    monkeypatch.setattr("builtins.input", lambda prompt="": "https://www.google.com/?code=TG-1")
    seen = {}

    def handler(request):
        seen["form"] = parse_qs(request.content.decode())
        return httpx.Response(
            200, json={"access_token": "fake-access-xyz", "refresh_token": "fake-refresh-new"}
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "ingestion.mercadolibre.auth.httpx.Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler)),
    )

    assert authorize.main() == 0
    out = capsys.readouterr().out
    assert out.count("fake-refresh-new") == 1
    assert "fake-access-xyz" not in out
    assert "fake-client-secret" not in out
    assert seen["form"]["code"] == ["TG-1"]
    assert seen["form"]["code_verifier"][0]
