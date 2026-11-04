"""Warehouse load helper for `.github/workflows/warehouse-load.yml` (task 2.6, ADR-017).

Two subcommands, both connecting with ``DATABASE_URL`` (never printed; errors name the exception
type only, because a libpq message can quote the host):

``plan --mode daily|full --olist-zip <path>``
    Decides which optional loads run and writes ``olist=true|false`` and ``enrichment=true|false``
    to ``$GITHUB_OUTPUT`` (booleans only). ``full`` runs both. ``daily`` runs ``raw_olist`` only
    when its input changed: the SHA-256 of any of the nine CSVs inside the archive differs from the
    latest ``raw_olist._load_audit`` row of its table, or a table / the audit is missing. It runs
    ``raw_enrichment`` only when it was never loaded (no audit row or table for ``fx_rates`` or
    ``public_holidays``); its inputs are fixed historical ranges, refreshed by a ``full`` run.
    Read-only on the database.

``summary --mode ... [--run-results <dbt run_results.json>] [--olist-manifest <manifest.json>]``
    Appends a Markdown summary to ``$GITHUB_STEP_SUMMARY``: row counts per raw table, ML days
    loaded, dbt result counts by resource type and status (plus the ids of failing nodes, which are
    project code, not data), ``pg_database_size`` and size per schema, and the Olist archive
    SHA-256 when it was loaded. Counts and names only: no row content, host, database name or URL.

Exit codes: 0 ok, 1 database or input error (the step fails loudly), 2 usage/configuration error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ingestion.db import ConfigError, connect, database_url_from_env
from ingestion.olist.files import AUDIT_TABLE as OLIST_AUDIT
from ingestion.olist.files import OLIST_FILES
from ingestion.olist.files import RAW_SCHEMA as OLIST_SCHEMA

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

RAW_SCHEMAS = ("raw_olist", "raw_enrichment", "raw_mercadolibre")
DBT_SCHEMAS = ("staging", "core", "marts")
ENRICHMENT_TABLES = ("fx_rates", "public_holidays")
CHUNK = 1 << 20
# Neon free plan: 1 GB storage per project (ADR-014). pg_database_size is a lower bound of what
# Neon bills (history/WAL retention comes on top), so warn early.
STORAGE_LIMIT_BYTES = 1_000_000_000
STORAGE_WARN_RATIO = 0.8
MAX_LISTED_NODES = 20


# --------------------------------------------------------------------------- plan


@dataclass
class Plan:
    olist: bool
    enrichment: bool
    reasons: list[str] = field(default_factory=list)


def archive_csv_hashes(zip_path: Path) -> dict[str, str] | None:
    """SHA-256 per Olist CSV inside the archive (streamed, nothing extracted); None if unreadable.

    Only the nine known member names are read. Any problem (not a zip, member missing, CRC error)
    returns None: the caller then runs the real loader, which fails with a precise message.
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            hashes: dict[str, str] = {}
            for spec in OLIST_FILES:
                digest = hashlib.sha256()
                with zf.open(spec.filename) as member:
                    while chunk := member.read(CHUNK):
                        digest.update(chunk)
                hashes[spec.table] = digest.hexdigest()
            return hashes
    except (OSError, KeyError, zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError):
        return None


def _exists(cur, schema: str, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f'"{schema}"."{table}"',))
    return bool(cur.fetchone()[0])


def loaded_olist_hashes(cur) -> dict[str, str] | None:
    """Latest ``file_sha256`` per table from ``raw_olist._load_audit``; None if never loaded."""
    if not _exists(cur, OLIST_SCHEMA, OLIST_AUDIT):
        return None
    if not all(_exists(cur, OLIST_SCHEMA, spec.table) for spec in OLIST_FILES):
        return None
    from psycopg import sql

    cur.execute(
        sql.SQL(
            "SELECT DISTINCT ON (table_name) table_name, file_sha256 FROM {} "
            "ORDER BY table_name, loaded_at DESC"
        ).format(sql.Identifier(OLIST_SCHEMA, OLIST_AUDIT))
    )
    return {name: sha.strip() for name, sha in cur.fetchall()}


def enrichment_loaded(cur) -> bool:
    if not _exists(cur, "raw_enrichment", "_load_audit"):
        return False
    if not all(_exists(cur, "raw_enrichment", t) for t in ENRICHMENT_TABLES):
        return False
    cur.execute("SELECT DISTINCT table_name FROM raw_enrichment._load_audit")
    found = {row[0] for row in cur.fetchall()}
    return all(t in found for t in ENRICHMENT_TABLES)


def decide(mode: str, zip_path: Path, cur) -> Plan:
    if mode == "full":
        return Plan(True, True, ["mode full: every raw schema is loaded"])
    plan = Plan(False, False)
    if not zip_path.is_file():
        plan.reasons.append(
            "::warning title=Olist archive missing::olist/archive.zip is not in the data repo; "
            "raw_olist was not checked"
        )
    else:
        archive = archive_csv_hashes(zip_path)
        loaded = loaded_olist_hashes(cur)
        if loaded is None:
            plan.olist = True
            plan.reasons.append("raw_olist: never loaded (or a table is missing): loading it")
        elif archive is None:
            plan.olist = True
            plan.reasons.append("raw_olist: archive unreadable: running the loader to report it")
        elif any(loaded.get(table) != sha for table, sha in archive.items()):
            plan.olist = True
            plan.reasons.append("raw_olist: archive content changed since the last load: loading")
        else:
            plan.reasons.append("raw_olist: unchanged (all nine CSV hashes match the last load)")
    if enrichment_loaded(cur):
        plan.reasons.append("raw_enrichment: already loaded (refresh with mode full)")
    else:
        plan.enrichment = True
        plan.reasons.append("raw_enrichment: never loaded (or a table is missing): loading it")
    return plan


def write_outputs(plan: Plan, environ: dict[str, str]) -> None:
    lines = f"olist={str(plan.olist).lower()}\nenrichment={str(plan.enrichment).lower()}\n"
    path = environ.get("GITHUB_OUTPUT", "")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lines)
    else:
        sys.stdout.write(lines)


# --------------------------------------------------------------------------- summary


def dbt_result_counts(run_results: Path) -> tuple[Counter, list[str], float | None] | None:
    """(count per (resource type, status), failing node ids, elapsed seconds); None if absent."""
    if not run_results.is_file():
        return None
    data = json.loads(run_results.read_text(encoding="utf-8"))
    counts: Counter = Counter()
    failing: list[str] = []
    for result in data.get("results", []):
        unique_id = str(result.get("unique_id", "?"))
        status = str(result.get("status", "?"))
        counts[(unique_id.split(".", 1)[0], status)] += 1
        if status in ("error", "fail", "warn", "runtime error"):
            failing.append(f"{unique_id} ({status})")
    elapsed = data.get("elapsed_time")
    return counts, failing, float(elapsed) if isinstance(elapsed, int | float) else None


def raw_row_counts(cur) -> list[tuple[str, str, int]]:
    from psycopg import sql

    cur.execute(
        "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p') ORDER BY 1, 2",
        (list(RAW_SCHEMAS),),
    )
    tables = cur.fetchall()
    counts = []
    for schema, table in tables:
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, table)))
        counts.append((schema, table, cur.fetchone()[0]))
    return counts


def ml_days(cur) -> tuple[int, str | None] | None:
    if not _exists(cur, "raw_mercadolibre", "runs"):
        return None
    cur.execute("SELECT count(*), max(snapshot_date)::text FROM raw_mercadolibre.runs")
    days, latest = cur.fetchone()
    return days, latest


def sizes(cur) -> tuple[int, list[tuple[str, int]]]:
    cur.execute("SELECT pg_database_size(current_database())")
    total = cur.fetchone()[0]
    cur.execute(
        "SELECT n.nspname, coalesce(sum(pg_total_relation_size(c.oid)), 0)::bigint"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'm', 'p')"
        " GROUP BY 1 ORDER BY 1",
        (list(RAW_SCHEMAS + DBT_SCHEMAS),),
    )
    return total, [(name, int(size)) for name, size in cur.fetchall()]


def _mb(n: int) -> str:
    return f"{n / 1_000_000:,.1f} MB"


def render_summary(
    *,
    mode: str,
    steps: dict[str, str],
    rows: list[tuple[str, str, int]] | None,
    days: tuple[int, str | None] | None,
    size: tuple[int, list[tuple[str, int]]] | None,
    dbt: tuple[Counter, list[str], float | None] | None,
    manifest: dict[str, Any] | None,
    db_error: str | None,
) -> tuple[str, list[str]]:
    """(Markdown, workflow annotations). Pure function: tested without a database."""
    out: list[str] = [f"### Warehouse load ({mode})", ""]
    notes: list[str] = []
    if steps:
        out += ["| step | outcome |", "|------|---------|"]
        out += [f"| {name} | {outcome or 'not run'} |" for name, outcome in steps.items()]
        out.append("")
    if db_error:
        out += [f"Database not reachable for the summary ({db_error}).", ""]
        notes.append(f"::error title=Warehouse summary::database not reachable ({db_error})")
    if rows is not None:
        out += ["#### Raw tables", "", "| table | rows |", "|-------|-----:|"]
        out += [f"| `{s}.{t}` | {n:,} |" for s, t, n in rows] or ["| (none) | 0 |"]
        out.append("")
    if days is not None:
        out += [f"ML snapshot days loaded: **{days[0]}** (latest {days[1] or '-'})", ""]
    if manifest is not None:
        sha = manifest.get("archive_sha256", "?")
        out += [f"Olist archive loaded this run: sha256 `{sha}`", ""]
    if dbt is None:
        out += ["dbt: no `run_results.json` (dbt did not run).", ""]
    else:
        counts, failing, elapsed = dbt
        took = f", {elapsed:.0f} s" if elapsed is not None else ""
        out += [f"#### dbt build{took}", "", "| resource | status | count |", "|---|---|---:|"]
        out += [f"| {kind} | {status} | {n} |" for (kind, status), n in sorted(counts.items())]
        out += ["| (none) | - | 0 |"] if not counts else []
        out.append("")
        if failing:
            shown = failing[:MAX_LISTED_NODES]
            more = len(failing) - len(shown)
            out += ["Not passing: " + ", ".join(f"`{x}`" for x in shown)]
            out += [f"(and {more} more)"] if more > 0 else []
            out.append("")
    if size is not None:
        total, per_schema = size
        ratio = total / STORAGE_LIMIT_BYTES
        out += [f"`pg_database_size`: **{_mb(total)}** ({ratio:.0%} of the 1 GB free plan)", ""]
        if per_schema:
            out += ["| schema | size |", "|--------|-----:|"]
            out += [f"| `{name}` | {_mb(n)} |" for name, n in per_schema]
            out.append("")
        if ratio >= STORAGE_WARN_RATIO:
            notes.append(
                f"::warning title=Warehouse storage::database is {_mb(total)}, "
                f"{ratio:.0%} of the Neon free-plan 1 GB (ADR-014)"
            )
    return "\n".join(out) + "\n", notes


def _parse_steps(items: list[str]) -> dict[str, str]:
    steps: dict[str, str] = {}
    for item in items:
        name, _, outcome = item.partition("=")
        steps[name] = outcome
    return steps


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.warehouse_report")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="decide the optional loads (GITHUB_OUTPUT)")
    plan.add_argument("--mode", choices=("daily", "full"), required=True)
    plan.add_argument("--olist-zip", type=Path, required=True)
    summary = sub.add_parser("summary", help="write the job summary")
    summary.add_argument("--mode", required=True)
    summary.add_argument("--run-results", type=Path)
    summary.add_argument("--olist-manifest", type=Path)
    summary.add_argument("--step", action="append", default=[], help="name=outcome")
    return parser


def _db_error(exc: BaseException) -> str:
    sqlstate = getattr(exc, "sqlstate", None)
    return type(exc).__name__ + (f", SQLSTATE {sqlstate}" if sqlstate else "")


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> int:
    environ = dict(os.environ) if environ is None else environ
    args = build_parser().parse_args(argv)
    try:
        url = database_url_from_env()
    except ConfigError as exc:
        print(f"::error::{exc}")
        return EXIT_CONFIG

    if args.command == "plan":
        try:
            with connect(url) as conn, conn.cursor() as cur:
                plan = decide(args.mode, args.olist_zip, cur)
        except Exception as exc:  # noqa: BLE001 - message may quote the host: type only
            print(f"::error title=Warehouse plan::database error ({_db_error(exc)})")
            return EXIT_FAILED
        for reason in plan.reasons:
            print(reason)
        write_outputs(plan, environ)
        return EXIT_OK

    rows = days = size = None
    db_error = None
    try:
        with connect(url) as conn, conn.cursor() as cur:
            rows = raw_row_counts(cur)
            days = ml_days(cur)
            size = sizes(cur)
    except Exception as exc:  # noqa: BLE001 - type only, see above
        db_error = _db_error(exc)
    dbt = None
    if args.run_results is not None:
        try:
            dbt = dbt_result_counts(args.run_results)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            print(
                f"::warning title=dbt results::run_results.json unreadable ({type(exc).__name__})"
            )
    manifest = None
    if args.olist_manifest is not None and args.olist_manifest.is_file():
        try:
            manifest = json.loads(args.olist_manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"::warning title=Olist manifest::unreadable ({type(exc).__name__})")
    text, notes = render_summary(
        mode=args.mode,
        steps=_parse_steps(args.step),
        rows=rows,
        days=days,
        size=size,
        dbt=dbt,
        manifest=manifest if isinstance(manifest, dict) else None,
        db_error=db_error,
    )
    for note in notes:
        print(note)
    path = environ.get("GITHUB_STEP_SUMMARY", "")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return EXIT_FAILED if db_error else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
