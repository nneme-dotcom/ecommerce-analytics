"""Fixtures for the warehouse tests: a throwaway database left empty after every test."""

from __future__ import annotations

import pytest


@pytest.fixture
def clean_db(database_url: str, monkeypatch):
    """An autocommit connection to the session's throwaway database, with ``DATABASE_URL`` set.

    Afterwards every user schema is dropped and an empty ``public`` re-created, so each test starts
    from a fresh database (raw_*, staging/core/marts, objects added to public).
    """
    import psycopg
    from psycopg import sql

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.delenv("FIXTURE_DB_ALLOW", raising=False)
    conn = psycopg.connect(database_url, autocommit=True)
    try:
        yield conn
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nspname FROM pg_namespace"
                " WHERE nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
                " AND nspname NOT LIKE 'pg\\_%'"
            )
            for (schema,) in cur.fetchall():
                cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            cur.execute("CREATE SCHEMA public")
        conn.close()
