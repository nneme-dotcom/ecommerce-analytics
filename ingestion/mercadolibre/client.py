"""Small, polite client for the Mercado Libre API.

- Bearer token in the ``Authorization`` header only (never in a URL).
- Sequential requests with a configurable delay between them.
- Retries with exponential backoff on 429 and 5xx (and any ``httpx.HTTPError``), at most
  ``max_retries``.
- A hard cap on HTTP requests per run (retries count towards it).
- Failed calls are recorded as ``{path, status}`` without payloads (``status`` is ``None`` for
  network errors such as timeouts). Tokens are never logged.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

import httpx

API_BASE_URL = "https://api.mercadolibre.com"
DEFAULT_DELAY_S = 0.5
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_REQUESTS = 1000
DEFAULT_BACKOFF_BASE_S = 1.0
MAX_BACKOFF_S = 60.0
DEFAULT_TIMEOUT_S = 30.0

logger = logging.getLogger(__name__)


class RequestCapReached(RuntimeError):
    """The per-run request cap was hit; no further requests are sent."""


def _is_retryable(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


class MercadoLibreClient:
    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = API_BASE_URL,
        delay_s: float = DEFAULT_DELAY_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not access_token:
            raise ValueError("access_token is required")
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=DEFAULT_TIMEOUT_S)
        self._headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        self._base_url = base_url.rstrip("/")
        self.delay_s = delay_s
        self.max_retries = max_retries
        self.max_requests = max_requests
        self.backoff_base_s = backoff_base_s
        self._sleep = sleep
        self.requests_made = 0
        self.errors: list[dict[str, Any]] = []

    def __repr__(self) -> str:  # never expose the token
        return (
            f"MercadoLibreClient(base_url={self._base_url!r}, requests_made={self.requests_made})"
        )

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> MercadoLibreClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        not_found_ok: bool = False,
    ) -> Any | None:
        """GET ``path`` and return decoded JSON, or ``None`` if the call ultimately failed.

        Failures (including a 200 whose body is not JSON or is JSON ``null``) are appended to
        ``self.errors``. With ``not_found_ok`` a 404 returns ``None`` without being recorded (used
        for end-of-data on paginated endpoints). Raises ``RequestCapReached`` when the cap would be
        exceeded.
        """
        if not path.startswith("/"):
            path = "/" + path
        url = self._base_url + path
        attempt = 0
        while True:
            status, response = self._send_once(url, path, params)
            if response is not None and status == 200:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if body is None:
                    logger.warning("GET %s status=200 but body is not JSON or is null", path)
                    self._record_error(path, status)
                return body
            if status == 404 and not_found_ok:
                logger.debug("GET %s status=404 (treated as end of data)", path)
                return None
            retryable = status is None or _is_retryable(status)
            if retryable and attempt < self.max_retries:
                wait = self._backoff(attempt, response)
                logger.info("GET %s status=%s retry=%d wait=%.1fs", path, status, attempt + 1, wait)
                self._sleep(wait)
                attempt += 1
                continue
            self._record_error(path, status)
            return None

    def _send_once(
        self, url: str, path: str, params: Mapping[str, Any] | None
    ) -> tuple[int | None, httpx.Response | None]:
        if self.requests_made >= self.max_requests:
            logger.warning("Request cap of %d reached; not sending GET %s", self.max_requests, path)
            raise RequestCapReached(f"request cap of {self.max_requests} reached")
        if self.requests_made > 0 and self.delay_s > 0:
            self._sleep(self.delay_s)
        self.requests_made += 1
        try:
            response = self._http.get(url, params=params, headers=self._headers)
        except httpx.HTTPError as exc:
            logger.warning("GET %s HTTP error: %s", path, type(exc).__name__)
            return None, None
        logger.debug("GET %s status=%s", path, response.status_code)
        return response.status_code, response

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                return min(float(retry_after), MAX_BACKOFF_S)
        return min(self.backoff_base_s * (2**attempt), MAX_BACKOFF_S)

    def record_error(self, path: str, status: int | None) -> None:
        """Record a failure that happened outside ``get`` (e.g. a malformed payload)."""
        self._record_error(path, status)

    def _record_error(self, path: str, status: int | None) -> None:
        logger.warning("GET %s failed status=%s", path, status)
        self.errors.append({"path": path, "status": status})

    def error_summary(self) -> list[dict[str, Any]]:
        """Aggregate recorded errors as ``{path, status, count}`` (the run.json shape)."""
        counts = Counter((e["path"], e["status"]) for e in self.errors)
        return [
            {"path": path, "status": status, "count": count}
            for (path, status), count in sorted(
                counts.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))
            )
        ]
