"""PostgreSQL connection helper shared by the loaders.

The connection string comes from the ``DATABASE_URL`` environment variable only. It may contain a
password, so it is never logged or printed: error messages name the variable, not its value.
"""

from __future__ import annotations

import os

DATABASE_URL_ENV = "DATABASE_URL"


class ConfigError(RuntimeError):
    """Required configuration (environment variables) is missing."""


def database_url_from_env() -> str:
    url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not url:
        raise ConfigError(
            f"{DATABASE_URL_ENV} is not set (e.g. postgresql://user@localhost:5432/ecommerce)"
        )
    return url


def connect(url: str):
    """Open a psycopg (v3) connection. Imported lazily so non-DB code never needs psycopg."""
    import psycopg

    return psycopg.connect(url, application_name="ecommerce-analytics-ingestion")
