"""OAuth token exchange for the Mercado Libre API.

Mercado Libre refresh tokens rotate on every use (ADR-004): every successful refresh returns a NEW
refresh token and invalidates the old one. Callers must persist ``TokenPair.refresh_token``.

Transient token-endpoint failures (429, 5xx, connect/timeout errors) are retried with exponential
backoff; if they persist, ``TransientAuthError`` is raised (the refresh token is probably still
valid, so the next run can try again). ``invalid_grant`` raises ``ReauthorizationRequired``.

Token values are never logged and never included in exception messages or ``repr``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

TOKEN_URL = "https://api.mercadolibre.com/oauth/token"  # noqa: S105 (a URL, not a secret)
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_S = 2.0

logger = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """Token endpoint call failed (network error, unexpected status or malformed response)."""

    def __init__(self, message: str, status: int | None = None, error: str | None = None):
        super().__init__(message)
        self.status = status
        self.error = error


class ReauthorizationRequired(AuthError):
    """The refresh token (or authorization code) was rejected. The owner must re-authorize."""


class TransientAuthError(AuthError):
    """The token endpoint kept failing with 429/5xx or network errors after all retries."""


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


def _is_transient(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _post_token(
    form: dict[str, str],
    http: httpx.Client | None,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> TokenPair:
    grant_type = form.get("grant_type", "?")
    own_client = http is None
    client = http or httpx.Client(timeout=DEFAULT_TIMEOUT_S)
    try:
        attempt = 0
        while True:
            try:
                response = client.post(
                    TOKEN_URL,
                    data=form,  # form-encoded body, never query string
                    headers={"Accept": "application/json"},
                )
                failure = f"HTTP {response.status_code}"
                transient = _is_transient(response.status_code)
            except httpx.HTTPError as exc:
                # Exception text from httpx does not contain the request body; log the type only.
                response = None
                failure = type(exc).__name__
                transient = True
            if not transient:
                break
            if attempt >= max_retries:
                logger.warning("POST /oauth/token grant_type=%s failed: %s", grant_type, failure)
                raise TransientAuthError(
                    f"Token endpoint unavailable after {max_retries} retries ({failure}). "
                    "The refresh token was probably not consumed: retry later. If the next run "
                    "gets invalid_grant, the owner must re-authorize.",
                    status=response.status_code if response is not None else None,
                )
            wait = backoff_base_s * (2**attempt)
            logger.info(
                "POST /oauth/token grant_type=%s %s; retry %d in %.1fs",
                grant_type,
                failure,
                attempt + 1,
                wait,
            )
            sleep(wait)
            attempt += 1
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
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> TokenPair:
    """Exchange a refresh token for a new access token AND a new (rotated) refresh token.

    Raises ``ReauthorizationRequired`` on ``invalid_grant``, ``TransientAuthError`` when 429/5xx/
    network errors persist after retries, and ``AuthError`` on anything else.
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
        max_retries=max_retries,
        backoff_base_s=backoff_base_s,
        sleep=sleep,
    )


def exchange_authorization_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    *,
    http: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
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
        sleep=sleep,
    )
