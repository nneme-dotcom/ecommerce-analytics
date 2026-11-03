"""Polite JSON GET client for the public enrichment APIs (no credentials involved).

Sequential requests, a small delay between calls, a hard request cap per run (retries count),
exponential backoff on 429/5xx and network errors (honouring a numeric ``Retry-After``).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

import httpx

DEFAULT_DELAY_S = 0.5
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_MAX_REQUESTS = 100
MAX_BACKOFF_S = 60.0
DEFAULT_TIMEOUT_S = 30.0

logger = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """A request failed for good (non-retryable status, retries exhausted, bad JSON, cap hit)."""


class JsonClient:
    def __init__(
        self,
        base_url: str,
        *,
        delay_s: float = DEFAULT_DELAY_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=DEFAULT_TIMEOUT_S, follow_redirects=True)
        self.delay_s = delay_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.max_requests = max_requests
        self._sleep = sleep
        self.requests_made = 0

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> JsonClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after and retry_after.strip().isdigit():
            return min(float(retry_after), MAX_BACKOFF_S)
        return min(self.backoff_base_s * 2**attempt, MAX_BACKOFF_S)

    def get_json(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        ok_statuses: tuple[int, ...] = (),
    ) -> tuple[int, Any]:
        """GET ``path``. Returns ``(200, parsed JSON)`` or ``(status, None)`` for ``ok_statuses``.

        Numbers with a fraction are parsed as ``Decimal`` (exact rates, no float rounding).
        """
        last = "no attempt"
        for attempt in range(self.max_retries + 1):
            if self.requests_made >= self.max_requests:
                raise FetchError(f"request cap reached ({self.max_requests})")
            if self.requests_made:
                self._sleep(self.delay_s)
            self.requests_made += 1
            retry_after = None
            try:
                resp = self._http.get(f"{self._base_url}{path}", params=params)
            except httpx.HTTPError as exc:
                last = f"network error ({type(exc).__name__})"
                logger.warning("GET %s: %s", path, last)
            else:
                logger.info("GET %s -> %d", path, resp.status_code)
                if resp.status_code == 200:
                    try:
                        return 200, json.loads(resp.content, parse_float=Decimal)
                    except ValueError as exc:
                        raise FetchError(f"GET {path}: response is not JSON") from exc
                if resp.status_code in ok_statuses:
                    return resp.status_code, None
                if resp.status_code != 429 and not 500 <= resp.status_code <= 599:
                    raise FetchError(f"GET {path}: HTTP {resp.status_code}")
                last = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")
            if attempt < self.max_retries:
                self._sleep(self._backoff(attempt, retry_after))
        raise FetchError(f"GET {path}: {last} after {self.max_retries + 1} attempts")
