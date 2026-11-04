"""Load the daily Mercado Libre snapshot folders (data repo) into PostgreSQL ``raw_mercadolibre``.

    DATABASE_URL=postgresql://... python -m ingestion.mercadolibre.load --source <data-repo-root>

Rules (DATA_CONTRACT §1b, ADR-015):
- Every ``mercadolibre/snapshot_date=YYYY-MM-DD/`` folder under ``--source`` is **validated
  before the database is touched**: ``run.json`` present and well-formed, the four ``.jsonl.gz``
  files valid gzip JSON Lines whose meta fields match the folder and ``run.json``, line counts equal
  to ``run.json`` ``row_counts``. Any bad folder fails the run (exit 1) with nothing changed.
- The whole run is **one transaction**. For each day to load: delete that ``snapshot_date`` from
  the five tables, ``COPY`` the files, check the loaded counts, append ``_load_audit`` rows.
- A day whose file SHA-256s equal the ones recorded in ``runs.file_sha256`` is unchanged and
  skipped, unless ``--full-reload`` is given or a data table was just (re-)created.
- Typed columns are lenient (wrong JSON type -> NULL); ``payload`` keeps the full line.

Error messages name files and line numbers, never line contents (the Actions logs are public, and
raw ML data stays private: ADR-003). ``DATABASE_URL`` is never logged.
Exit codes: 0 ok · 1 validation or load failed (nothing changed) · 2 configuration error.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import re
import sys
import uuid
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ingestion.db import ConfigError, connect, database_url_from_env
from ingestion.mercadolibre.raw_schema import (
    AUDIT_TABLE,
    COMMON_HEAD,
    DATA_TABLES,
    FILE_FIELDS,
    FILE_TABLES,
    PAYLOAD,
    RUN_FIELDS,
    RUN_LOADER_COLUMNS,
    RUNS_TABLE,
    SCHEMA,
    Column,
    SchemaDriftError,
    ensure_schema,
)

EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2

ML_DIR = "mercadolibre"
DAY_PREFIX = "snapshot_date="
RUN_FILE = "run.json"
STATUSES = ("success", "partial", "failed")
INT_RANGES = {"integer": (-(2**31), 2**31 - 1), "bigint": (-(2**63), 2**63 - 1)}
# psycopg COPY type names; payload is sent as text and parsed by the server into jsonb, so the
# stored document is the line exactly as written (numbers keep their precision).
COPY_TYPES = {"timestamp with time zone": "timestamptz", "jsonb": "text"}
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

logger = logging.getLogger(__name__)


class LoadError(RuntimeError):
    """A folder failed validation or the database load failed (nothing was committed)."""


def file_name(kind: str) -> str:
    return f"{kind}.jsonl.gz"


# --------------------------------------------------------------------------- value conversion


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _int(value: Any, col_type: str) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    low, high = INT_RANGES[col_type]
    return value if low <= value <= high else None


def _numeric(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal) and value.is_finite():
        return value
    return None


def _str_list(value: Any) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    return None


def convert(col_type: str, value: Any) -> Any:
    """JSON value -> typed column value, or None if absent / of the wrong JSON type."""
    if value is None:
        return None
    if col_type == "text":
        return value if isinstance(value, str) else None
    if col_type in INT_RANGES:
        return _int(value, col_type)
    if col_type == "numeric":
        return _numeric(value)
    if col_type == "boolean":
        return value if isinstance(value, bool) else None
    if col_type == "text[]":
        return _str_list(value)
    if col_type == "timestamp with time zone":
        return _timestamp(value)
    if col_type == "jsonb":
        return value
    raise ValueError(f"no converter for {col_type}")


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _has_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_nul(v) for v in value)
    return False


def parse_json(text: str, where: str, *, exact: bool) -> Any:
    """Strict JSON: no NaN/Infinity, no NUL characters (jsonb refuses them)."""
    try:
        obj = json.loads(
            text, parse_float=Decimal if exact else float, parse_constant=_reject_constant
        )
    except ValueError:
        raise LoadError(f"{where}: blank line or not valid JSON") from None
    if "\\u0000" in text and _has_nul(obj):
        raise LoadError(f"{where}: contains a NUL character (not storable in jsonb)")
    return obj


def _uuid(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- validation


@dataclass(frozen=True)
class DayFolder:
    snapshot_date: date
    path: Path
    run_id: uuid.UUID
    status: str
    run_meta: dict[str, Any]
    row_counts: dict[str, int]
    sha256: dict[str, str]  # file name -> hex digest (run.json and the four files)


def iter_rows(
    kind: str, data: bytes, snapshot_date: date, run_id: uuid.UUID, folder: str
) -> Iterator[tuple[Any, ...]]:
    """Yield the COPY tuples of one ``.jsonl.gz`` file; raise :class:`LoadError` on any problem."""
    name = file_name(kind)
    try:
        text = gzip.decompress(data)
    except (OSError, EOFError, zlib.error) as exc:
        raise LoadError(f"{folder}/{name}: not a valid gzip file ({type(exc).__name__})") from None
    lines = text.split(b"\n")
    if lines[-1] == b"":
        lines.pop()  # trailing newline after the last record
    fields = FILE_FIELDS[kind]
    day_iso = snapshot_date.isoformat()
    for line_no, raw in enumerate(lines, start=1):
        where = f"{folder}/{name}:{line_no}"
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise LoadError(f"{where}: not UTF-8") from None
        obj = parse_json(line, where, exact=True)
        if not isinstance(obj, dict):
            raise LoadError(f"{where}: not a JSON object")
        if obj.get("snapshot_date") != day_iso:
            raise LoadError(f"{where}: snapshot_date does not match the folder")
        if _uuid(obj.get("run_id")) != run_id:
            raise LoadError(f"{where}: run_id does not match {RUN_FILE}")
        captured_at = _timestamp(obj.get("captured_at"))
        if captured_at is None:
            raise LoadError(f"{where}: captured_at missing or not an ISO-8601 timestamp")
        typed = []
        for col in fields:
            value = convert(col.type, obj.get(col.name))
            if col.required and value is None:
                raise LoadError(f"{where}: required field {col.name} missing or not {col.type}")
            typed.append(value)
        yield (snapshot_date, run_id, line_no, captured_at, *typed, line)


def _read_run_meta(day_dir: Path, snapshot_date: date) -> tuple[dict[str, Any], bytes]:
    folder = day_dir.name
    path = day_dir / RUN_FILE
    if not path.is_file():
        raise LoadError(f"{folder}: {RUN_FILE} is missing")
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise LoadError(f"{folder}/{RUN_FILE}: not UTF-8") from None
    meta = parse_json(text, f"{folder}/{RUN_FILE}", exact=False)
    if not isinstance(meta, dict):
        raise LoadError(f"{folder}/{RUN_FILE}: not a JSON object")
    if _uuid(meta.get("run_id")) is None:
        raise LoadError(f"{folder}/{RUN_FILE}: run_id missing or not a uuid")
    if meta.get("snapshot_date") != snapshot_date.isoformat():
        raise LoadError(f"{folder}/{RUN_FILE}: snapshot_date does not match the folder")
    if meta.get("status") not in STATUSES:
        raise LoadError(f"{folder}/{RUN_FILE}: status is not one of {', '.join(STATUSES)}")
    counts = meta.get("row_counts")
    if not isinstance(counts, dict) or any(
        isinstance(counts.get(k), bool) or not isinstance(counts.get(k), int) or counts[k] < 0
        for k in FILE_TABLES
    ):
        raise LoadError(
            f"{folder}/{RUN_FILE}: row_counts must hold non-negative ints for "
            + ", ".join(FILE_TABLES)
        )
    return meta, data


def validate_day(day_dir: Path) -> DayFolder:
    """Fully validate one day folder (no database access)."""
    folder = day_dir.name
    day_text = folder[len(DAY_PREFIX) :]
    try:
        if not DAY_RE.fullmatch(day_text):
            raise ValueError
        snapshot_date = date.fromisoformat(day_text)
    except ValueError:
        raise LoadError(f"{folder}: folder name is not snapshot_date=YYYY-MM-DD") from None
    meta, run_bytes = _read_run_meta(day_dir, snapshot_date)
    run_id = uuid.UUID(meta["run_id"])
    sha = {RUN_FILE: hashlib.sha256(run_bytes).hexdigest()}
    for kind in FILE_TABLES:
        path = day_dir / file_name(kind)
        if not path.is_file():
            raise LoadError(f"{folder}: {file_name(kind)} is missing")
        data = path.read_bytes()
        sha[file_name(kind)] = hashlib.sha256(data).hexdigest()
        rows = sum(1 for _ in iter_rows(kind, data, snapshot_date, run_id, folder))
        expected = meta["row_counts"][kind]
        if rows != expected:
            raise LoadError(
                f"{folder}/{file_name(kind)}: {rows} lines but {RUN_FILE} row_counts"
                f" says {expected}"
            )
    return DayFolder(
        snapshot_date=snapshot_date,
        path=day_dir,
        run_id=run_id,
        status=meta["status"],
        run_meta=meta,
        row_counts={k: meta["row_counts"][k] for k in FILE_TABLES},
        sha256=sha,
    )


def discover(source: Path) -> list[Path]:
    base = source / ML_DIR
    if not base.is_dir():
        raise LoadError(f"no {ML_DIR}/ folder under --source")
    days = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith((".tmp-", ".old-")):
            logger.warning("skipping leftover folder %s (killed snapshot run)", entry.name)
        elif entry.name.startswith(DAY_PREFIX):
            days.append(entry)
        else:
            logger.warning("skipping unexpected folder %s", entry.name)
    if not days:
        raise LoadError(f"no {DAY_PREFIX}YYYY-MM-DD folders under {ML_DIR}/")
    return days


def prepare(source: Path) -> list[DayFolder]:
    """Validate every day folder. Raises :class:`LoadError` on the first bad one."""
    prepared = [validate_day(d) for d in discover(source)]
    logger.info("validated %d day folders", len(prepared))
    return prepared


# --------------------------------------------------------------------------- database load


@dataclass
class LoadSummary:
    load_id: uuid.UUID
    loaded: list[date] = field(default_factory=list)
    unchanged: list[date] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=lambda: {k: 0 for k in FILE_TABLES})


def _copy_type(col: Column) -> str:
    return COPY_TYPES.get(col.type, col.type)


def _count(cur, table: str, snapshot_date: date) -> int:
    from psycopg import sql

    cur.execute(
        sql.SQL("SELECT count(*) FROM {} WHERE snapshot_date = %s").format(
            sql.Identifier(SCHEMA, table)
        ),
        (snapshot_date,),
    )
    return cur.fetchone()[0]


def _insert_run(cur, day: DayFolder, load_id: uuid.UUID) -> None:
    from psycopg import sql
    from psycopg.types.json import Jsonb

    values: list[Any] = []
    for col in RUN_FIELDS:
        if col.name == "snapshot_date":
            values.append(day.snapshot_date)
        elif col.name == "run_id":
            values.append(day.run_id)
        else:
            value = convert(col.type, day.run_meta.get(col.name))
            values.append(Jsonb(value) if col.type == "jsonb" and value is not None else value)
    values += [Jsonb(day.run_meta), load_id, Jsonb(day.sha256)]
    columns = RUN_FIELDS + RUN_LOADER_COLUMNS
    cur.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(SCHEMA, RUNS_TABLE),
            sql.SQL(", ").join(sql.Identifier(c.name) for c in columns),
            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        ),
        values,
    )


def _audit(cur, load_id, day: DayFolder, table: str, source_file: str, rows: int) -> None:
    from psycopg import sql

    cur.execute(
        sql.SQL(
            "INSERT INTO {} (load_id, snapshot_date, table_name, run_id, source_file, row_count,"
            " file_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s)"
        ).format(sql.Identifier(SCHEMA, AUDIT_TABLE)),
        (load_id, day.snapshot_date, table, day.run_id, source_file, rows, day.sha256[source_file]),
    )


def _load_day(cur, day: DayFolder, load_id: uuid.UUID, summary: LoadSummary) -> None:
    from psycopg import sql

    folder = day.path.name
    for table in DATA_TABLES:
        cur.execute(
            sql.SQL("DELETE FROM {} WHERE snapshot_date = %s").format(
                sql.Identifier(SCHEMA, table)
            ),
            (day.snapshot_date,),
        )
    _insert_run(cur, day, load_id)
    _audit(cur, load_id, day, RUNS_TABLE, RUN_FILE, 1)
    for kind in FILE_TABLES:
        name = file_name(kind)
        data = (day.path / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != day.sha256[name]:
            raise LoadError(f"{folder}/{name}: file changed after validation")
        columns = COMMON_HEAD + FILE_FIELDS[kind] + (PAYLOAD,)
        copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(
            sql.Identifier(SCHEMA, kind),
            sql.SQL(", ").join(sql.Identifier(c.name) for c in columns),
        )
        with cur.copy(copy_sql) as copy:
            copy.set_types([_copy_type(c) for c in columns])
            for row in iter_rows(kind, data, day.snapshot_date, day.run_id, folder):
                copy.write_row(row)
        loaded = _count(cur, kind, day.snapshot_date)
        if loaded != day.row_counts[kind]:
            raise LoadError(
                f"{folder}/{name}: loaded {loaded} rows but {RUN_FILE} row_counts says "
                f"{day.row_counts[kind]}"
            )
        _audit(cur, load_id, day, kind, name, loaded)
        summary.rows[kind] += loaded


def load(conn, days: list[DayFolder], *, full_reload: bool = False) -> LoadSummary:
    """Load the validated days in one transaction; skip unchanged ones unless ``full_reload``."""
    from psycopg import sql

    summary = LoadSummary(load_id=uuid.uuid4())
    with conn.transaction(), conn.cursor() as cur:
        fresh = ensure_schema(cur)
        if fresh and not full_reload:
            logger.info("tables created or re-created (%s): loading every day", sorted(fresh))
        reload_all = full_reload or bool(fresh)
        cur.execute(
            sql.SQL("SELECT snapshot_date, file_sha256 FROM {}").format(
                sql.Identifier(SCHEMA, RUNS_TABLE)
            )
        )
        loaded_sha = dict(cur.fetchall())
        orphans = sorted(set(loaded_sha) - {d.snapshot_date for d in days})
        if orphans:
            logger.warning(
                "%d loaded day(s) have no folder under --source and are kept: %s",
                len(orphans),
                ", ".join(d.isoformat() for d in orphans),
            )
        for day in days:
            if not reload_all and loaded_sha.get(day.snapshot_date) == day.sha256:
                summary.unchanged.append(day.snapshot_date)
                continue
            _load_day(cur, day, summary.load_id, summary)
            summary.loaded.append(day.snapshot_date)
            logger.info(
                "loaded %s (%s): %s",
                day.snapshot_date,
                day.status,
                ", ".join(f"{k} {day.row_counts[k]}" for k in FILE_TABLES),
            )
    return summary


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ingestion.mercadolibre.load",
        description="Load the ML snapshot folders into PostgreSQL raw_mercadolibre "
        "(needs DATABASE_URL).",
    )
    parser.add_argument(
        "--source", type=Path, required=True, help="data repo root (holds mercadolibre/)"
    )
    parser.add_argument(
        "--full-reload", action="store_true", help="reload every day, even unchanged ones"
    )
    return parser


def describe_db_error(exc: Exception) -> str:
    """A loggable description of a psycopg error that never echoes row values.

    Data exceptions (SQLSTATE class 22) and integrity violations (23) may quote the offending
    value or the COPY line, so only their class and SQLSTATE are reported.
    """
    sqlstate = getattr(exc, "sqlstate", None) or "?"
    name = type(exc).__name__
    if sqlstate[:2] in ("22", "23"):
        return f"{name} (SQLSTATE {sqlstate})"
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    return f"{name} (SQLSTATE {sqlstate}): {primary or 'no message'}"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        url = database_url_from_env()
    except ConfigError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    try:
        days = prepare(args.source)
    except LoadError as exc:
        logger.error("validation failed, nothing loaded: %s", exc)
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
            summary = load(conn, days, full_reload=args.full_reload)
    except (LoadError, SchemaDriftError) as exc:
        logger.error("load rolled back, nothing changed: %s", exc)
        return EXIT_FAILED
    except psycopg.Error as exc:
        logger.error("load rolled back, nothing changed: %s", describe_db_error(exc))
        return EXIT_FAILED
    logger.info(
        "load %s committed: %d day(s) loaded, %d unchanged; rows %s",
        summary.load_id,
        len(summary.loaded),
        len(summary.unchanged),
        summary.rows,
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
