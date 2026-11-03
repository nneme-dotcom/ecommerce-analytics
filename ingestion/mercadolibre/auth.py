"""OAuth token exchange for the Mercado Libre API.

Mercado Libre refresh tokens rotate on every use (ADR-004): every successful refresh returns a NEW
refresh token and invalidates the old one. Callers must persist ``TokenPair.refresh_token``.

Token values are never logged and never included in exception messages or ``repr``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

TOKEN_URL = "https://api.mercadolibre.com/oauth/token"  # noqa: S105 (a URL, not a secret)
DEFAULT_TIMEOUT_S = 30.0

logger = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """Token endpoint call failed (network error, unexpected status or malformed response)."""

    def __init__(self, message: str, status: int | None = None, error: str | None = None):
        super().__init__(message)
        self.status = status
        self.error = error


class ReauthorizationRequired(AuthError):
    """The refresh token (or authorization code) was rejected. The owner must re-authorize."""


REAUTHORIZE_HINT = (
    "The Mercado Libre refresh token was rejected ({error}). It may have expired, been revoked, "
    "or already been used (tokens rotate on every refresh). The account owner must re-authorize: "
    "run `python -m ingestion.mercadolibre.authorize` and store the new refresh token as the "
    "ML_REFRESH_TOKEN secret."
)


@dataclass(frozen=True)
class TokenPair:
    """Result of a token exchange. ``repr`` hides the token values."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int | None = None
    user_id: int | None = None
    scope: str | None = None


def _post_token(form: dict[str, str], http: httpx.Client | None) -> TokenPair:
    grant_type = form.get("grant_type", "?")
    own_client = http is None
    client = http or httpx.Client(timeout=DEFAULT_TIMEOUT_S)
    try:
        try:
            response = client.post(
                TOKEN_URL,
                data=form,  # form-encoded body, never query string
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            # Exception text from httpx does not contain the request body.
            raise AuthError(f"Token request failed: {type(exc).__name__}") from None
    finally:
        if own_client:
            client.close()

    logger.info("POST /oauth/token grant_type=%s status=%s", grant_type, response.status_code)

    error_code = _error_code(response)
    if response.status_code in (400, 401) and error_code == "invalid_grant":
        raise ReauthorizationRequired(
            REAUTHORIZE_HINT.format(error=error_code), status=response.status_code, error=error_code
        )
    if response.status_code != 200:
        raise AuthError(
            f"Token endpoint returned HTTP {response.status_code} "
            f"({error_code or 'no error code'})",
            status=response.status_code,
            error=error_code,
        )

    try:
        payload = response.json()
        access_token = payload["access_token"]
        refresh_token = payload["refresh_token"]
    except (ValueError, KeyError, TypeError):
        raise AuthError(
            "Token endpoint returned 200 without access_token/refresh_token", status=200
        ) from None
    if not access_token or not refresh_token:
        raise AuthError("Token endpoint returned empty tokens", status=200)

    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=payload.get("expires_in"),
        user_id=payload.get("user_id"),
        scope=payload.get("scope"),
    )


def _error_code(response: httpx.Response) -> str | None:
    """Return the OAuth ``error`` code (e.g. ``invalid_grant``) only, never the whole body."""
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return body["error"]
    return None


def refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    *,
    http: httpx.Client | None = None,
) -> TokenPair:
    """Exchange a refresh token for a new access token AND a new (rotated) refresh token.

    Raises ``ReauthorizationRequired`` on ``invalid_grant`` and ``AuthError`` on anything else.
    """
    if not (client_id and client_secret and refresh_token):
        raise AuthError("client_id, client_secret and refresh_token are all required")
    return _post_token(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        http,
    )


def exchange_authorization_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    *,
    http: httpx.Client | None = None,
) -> TokenPair:
    """Exchange a one-time authorization code (PKCE S256) for the first token pair."""
    return _post_token(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        http,
    )
