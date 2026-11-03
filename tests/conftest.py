"""Shared fixtures: a throwaway PostgreSQL database for loader tests.

- ``DATABASE_URL`` set (CI, or a developer's server): a fresh database ``ecomm_test_<random>`` is
  created on that server for the session and dropped afterwards, so tests never touch real tables.
  The role needs CREATEDB. A configured but unreachable server fails the tests (no silent skip).
- Not set: if PostgreSQL server binaries are found (``PG_BIN``, ``PATH`` or
  ``/usr/lib/postgresql/<v>/bin``), a temporary cluster is started on a free localhost port.
- Otherwise (or as root, which ``initdb`` refuses): DB tests are skipped with the reason.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest


def _find_pg_bindir() -> Path | None:
    candidates: list[Path] = []
    if os.environ.get("PG_BIN"):
        candidates.append(Path(os.environ["PG_BIN"]))
    on_path = shutil.which("initdb")
    if on_path:
        candidates.append(Path(on_path).parent)
    candidates += sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True)
    for bindir in candidates:
        if (bindir / "initdb").is_file() and (bindir / "pg_ctl").is_file():
            return bindir
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def _pg_server_url(tmp_path_factory) -> Iterator[str]:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        yield url
        return
    bindir = _find_pg_bindir()
    if bindir is None:
        pytest.skip("no DATABASE_URL and no PostgreSQL binaries found")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("initdb refuses to run as root: set DATABASE_URL to run DB tests")
    datadir = tmp_path_factory.mktemp("pgdata")
    log = datadir.parent / "postgres.log"
    port = _free_port()
    subprocess.run(  # noqa: S603 - fixed binaries, no shell
        [
            str(bindir / "initdb"),
            "-D",
            str(datadir),
            "-U",
            "postgres",
            "-A",
            "trust",
            "-E",
            "UTF8",
            "--locale=C",
            "--no-sync",
        ],
        check=True,
        capture_output=True,
    )
    opts = f"-c listen_addresses=127.0.0.1 -p {port} -c unix_socket_directories='' -c fsync=off"
    subprocess.run(  # noqa: S603
        [str(bindir / "pg_ctl"), "-D", str(datadir), "-l", str(log), "-o", opts, "-w", "start"],
        check=True,
        capture_output=True,
    )
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run(  # noqa: S603
            [str(bindir / "pg_ctl"), "-D", str(datadir), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )


@pytest.fixture(scope="session")
def database_url(_pg_server_url: str) -> Iterator[str]:
    """Connection string of an empty, throwaway database (dropped at the end of the session)."""
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    name = f"ecomm_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_pg_server_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(_pg_server_url, dbname=name)
    finally:
        with psycopg.connect(_pg_server_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def db_conn(database_url: str):
    """A connection to the throwaway database; every schema it creates is dropped afterwards."""
    import psycopg
    from psycopg import sql

    conn = psycopg.connect(database_url)
    try:
        yield conn
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'raw\\_%' ESCAPE '\\'")
            for (schema,) in cur.fetchall():
                cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        conn.commit()
        conn.close()
