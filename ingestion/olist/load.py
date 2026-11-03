"""Load the nine Olist CSVs, unchanged, into the PostgreSQL schema ``raw_olist``.

    DATABASE_URL=postgresql://... python -m ingestion.olist.load --source data/olist

Rules (DATA_CONTRACT §2):
- Every column is ``TEXT``, named exactly as the CSV header; typing happens in ``staging``.
- Before touching the database, each file is checked: header equals the contract, every record has
  the expected number of fields, and (if ``manifest.json`` from the download step exists) its
  SHA-256 matches the manifest. Records are counted with a CSV parser, so review comments with
  quoted newlines count once.
- The whole load is **one transaction**: for each table, create it if missing (re-create it if its
  columns differ from the contract), ``TRUNCATE``, ``COPY ... FROM STDIN (FORMAT csv, HEADER)``,
  compare the loaded row count with the parsed count, append one ``raw_olist._load_audit`` row.
  Any failure rolls everything back, so the schema always holds one complete, consistent load.
  Re-running with the same files gives the same tables (idempotent); only the audit grows.
- In CSV format PostgreSQL loads an unquoted empty field as NULL and a quoted empty string as ''.

``DATABASE_URL`` is never logged.
Exit codes: 0 ok · 1 load failed (nothing changed) · 2 configuration error.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ingestion.db import ConfigError, connect, database_url_from_env
from ingestion.olist.download import CHUNK, sha256_file
from ingestion.olist.files import AUDIT_TABLE, MANIFEST_NAME, OLIST_FILES, RAW_SCHEMA, OlistFile

EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2

logger = logging.getLogger(__name__)


class LoadError(RuntimeError):
    """A source file failed validation or the database load failed (nothing was committed)."""


@dataclass(frozen=True)
class PreparedFile:
    spec: OlistFile
    path: Path
    sha256: str
    rows: int


def read_header(path: Path) -> list[str]:
    # utf-8-sig: tolerate a byte-order mark before the first column name.
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return next(csv.reader(fh), [])


def count_records(path: Path, n_columns: int) -> int:
    """Data records (header excluded), parsed as CSV; fails on a record with the wrong arity."""
    rows = 0
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh, strict=True)
        next(reader, None)
        for record in reader:
            if len(record) != n_columns:
                raise LoadError(
                    f"{path.name}: record ending on line {reader.line_num} has {len(record)} "
                    f"fields, expected {n_columns}"
                )
            rows += 1
    return rows


def _read_manifest(source: Path) -> dict[str, Any] | None:
    path = source / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        files = json.loads(path.read_text(encoding="utf-8"))["files"]
    except (ValueError, KeyError, TypeError) as exc:
        raise LoadError(f"{MANIFEST_NAME} is unreadable ({type(exc).__name__})") from exc
    if not isinstance(files, dict):
        raise LoadError(f"{MANIFEST_NAME}: 'files' is not an object")
    return files


def prepare(source: Path) -> list[PreparedFile]:
    """Validate all nine files (no database access). Raises ``LoadError`` on the first problem."""
    missing = [s.filename for s in OLIST_FILES if not (source / s.filename).is_file()]
    if missing:
        raise LoadError(f"missing files in {source}: {missing}")
    manifest = _read_manifest(source)
    if manifest is None:
        logger.warning("no %s in %s: file checksums are not verified", MANIFEST_NAME, source)
    prepared: list[PreparedFile] = []
    for spec in OLIST_FILES:
        path = source / spec.filename
        header = read_header(path)
        if tuple(header) != spec.columns:
            raise LoadError(
                f"{spec.filename}: header {header} does not match the contract {list(spec.columns)}"
            )
        digest = sha256_file(path)
        if manifest is not None:
            expected = (manifest.get(spec.filename) or {}).get("sha256")
            if expected != digest:
                raise LoadError(f"{spec.filename}: SHA-256 does not match {MANIFEST_NAME}")
        try:
            rows = count_records(path, len(spec.columns))
        except (csv.Error, UnicodeDecodeError) as exc:
            raise LoadError(f"{spec.filename}: not valid UTF-8 CSV ({exc})") from exc
        prepared.append(PreparedFile(spec, path, digest, rows))
    return prepared


def _ensure_table(cur, spec: OlistFile) -> None:
    from psycopg import sql

    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (RAW_SCHEMA, spec.table),
    )
    existing = tuple(row[0] for row in cur.fetchall())
    if existing == spec.columns:
        return
    table = sql.Identifier(RAW_SCHEMA, spec.table)
    if existing:
        # Contract changed. No CASCADE: if a view depends on the table, fail loudly instead.
        logger.info("re-creating %s.%s (columns changed)", RAW_SCHEMA, spec.table)
        cur.execute(sql.SQL("DROP TABLE {}").format(table))
    columns = sql.SQL(", ").join(sql.SQL("{} text").format(sql.Identifier(c)) for c in spec.columns)
    cur.execute(sql.SQL("CREATE TABLE {} ({})").format(table, columns))


def load(conn, prepared: list[PreparedFile]) -> uuid.UUID:
    """Load every prepared file in one transaction. Returns the ``load_id``."""
    from psycopg import sql

    load_id = uuid.uuid4()
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(RAW_SCHEMA)))
        cur.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} ("
                " load_id uuid NOT NULL,"
                " table_name text NOT NULL,"
                " source_file text NOT NULL,"
                " row_count bigint NOT NULL,"
                " file_sha256 char(64) NOT NULL,"
                " loaded_at timestamptz NOT NULL DEFAULT now(),"
                " PRIMARY KEY (load_id, table_name))"
            ).format(sql.Identifier(RAW_SCHEMA, AUDIT_TABLE))
        )
        for item in prepared:
            spec = item.spec
            table = sql.Identifier(RAW_SCHEMA, spec.table)
            _ensure_table(cur, spec)
            cur.execute(sql.SQL("TRUNCATE {}").format(table))
            copy_sql = sql.SQL(
                "COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')"
            ).format(table, sql.SQL(", ").join(sql.Identifier(c) for c in spec.columns))
            with cur.copy(copy_sql) as copy, item.path.open("rb") as fh:
                while chunk := fh.read(CHUNK):
                    copy.write(chunk)
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(table))
            loaded = cur.fetchone()[0]
            if loaded != item.rows:
                raise LoadError(
                    f"{spec.filename}: loaded {loaded} rows but the CSV has {item.rows} records"
                )
            cur.execute(
                sql.SQL(
                    "INSERT INTO {} (load_id, table_name, source_file, row_count, file_sha256)"
                    " VALUES (%s, %s, %s, %s, %s)"
                ).format(sql.Identifier(RAW_SCHEMA, AUDIT_TABLE)),
                (load_id, spec.table, spec.filename, loaded, item.sha256),
            )
            logger.info("loaded %s.%s: %d rows", RAW_SCHEMA, spec.table, loaded)
    return load_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ingestion.olist.load",
        description="Load the Olist CSVs into PostgreSQL schema raw_olist (needs DATABASE_URL).",
    )
    parser.add_argument("--source", type=Path, default=Path("data/olist"), help="CSV folder")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        url = database_url_from_env()
    except ConfigError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    try:
        prepared = prepare(args.source)
    except LoadError as exc:
        logger.error("%s", exc)
        return EXIT_FAILED

    import psycopg

    try:
        conn = connect(url)
    except psycopg.Error as exc:
        # The message of a malformed connection string may echo part of it: don't print it.
        logger.error(
            "could not connect to the database (%s); check DATABASE_URL", type(exc).__name__
        )
        return EXIT_FAILED
    try:
        with conn:
            load_id = load(conn, prepared)
    except (LoadError, psycopg.Error) as exc:
        logger.error("load rolled back, nothing changed: %s", exc)
        return EXIT_FAILED
    total = sum(p.rows for p in prepared)
    logger.info("load %s committed: %d tables, %d rows", load_id, len(prepared), total)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
