"""Prepare a throwaway PostgreSQL for the CI ``dbt build`` and check what dbt did to it (ADR-016).

    FIXTURE_DB_ALLOW=1 DATABASE_URL=postgresql://... python -m warehouse.ci.fixture_db load \
        --inventory <file>
    dbt build --project-dir warehouse --profiles-dir warehouse
    DATABASE_URL=postgresql://... python -m warehouse.ci.fixture_db check --inventory <file>

``load`` fills ``raw_olist`` and ``raw_enrichment`` from ``warehouse/fixtures/`` with the **real
loaders** (``ingestion.olist.load``, ``ingestion.enrichment.fx_rates`` / ``holidays``), so the raw
tables have exactly the loaders' DDL and the fixtures pass the loaders' own validation. dbt seeds
are not used: dbt never writes into a ``raw_*`` schema. It then writes an inventory (below).

``load`` replaces raw table contents (Olist ``TRUNCATE``, enrichment delete-and-insert), so it
refuses to run (exit 2, nothing written) unless

- the environment variable ``FIXTURE_DB_ALLOW`` is ``1`` (set only in the CI ``dbt`` job), and
- no table it writes already holds more rows than the fixtures put there (e.g. ``raw_olist.orders``
  with the ~100k real orders): a real load is never truncated, even with the opt-in set.

The inventory covers every ``raw_*`` schema and ``public``:

- relations (tables, views, matviews, indexes, sequences, ...): kind; for tables, views and
  matviews the column signature (names, order, ``format_type()`` types), the row count and an md5
  of all rows in a stable order (rows as text, sorted); for views and indexes their definition;
- constraints and triggers on those relations (definition);
- functions/procedures (identity arguments, result type, md5 of the body) and standalone types.

``check`` runs after ``dbt build`` and fails if dbt created any schema other than ``staging``,
``core`` and ``marts``, or if anything in the inventory was added, dropped or changed (a same-count
``UPDATE``, an ``ADD COLUMN``, a function in ``raw_olist``, a table in ``public``, ...).

``DATABASE_URL`` is never logged. Exit codes: 0 ok · 1 load or check failed · 2 configuration
error or refused (opt-in missing, database already holds more than the fixtures).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ingestion.db import ConfigError, connect, database_url_from_env
from ingestion.enrichment import fx_rates, holidays
from ingestion.enrichment.http import FetchError
from ingestion.enrichment.store import SchemaDriftError
from ingestion.olist import load as olist_load
from ingestion.olist.files import RAW_SCHEMA as OLIST_SCHEMA

EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2

ALLOW_ENV = "FIXTURE_DB_ALLOW"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
DBT_SCHEMAS = frozenset({"staging", "core", "marts"})
# Schemas that exist before dbt runs and that dbt must leave alone (besides raw_*).
BASE_SCHEMAS = frozenset({"public"})
ENRICHMENT_SCHEMA = "raw_enrichment"
# Relation kinds whose rows are read and hashed (tables, partitioned tables, views, matviews).
ROW_KINDS = frozenset({"r", "p", "v", "m"})
# Schemas covered by the inventory: every raw_* schema, and public. Parameters of each inventory
# query's "(n.nspname LIKE %s OR n.nspname = %s)" filter (LIKE's default escape is backslash).
INVENTORY_SCHEMAS = ("raw\\_%", "public")

logger = logging.getLogger(__name__)


class CheckError(RuntimeError):
    """dbt created an unexpected schema or changed a raw schema or ``public``."""


class RefusedError(RuntimeError):
    """``load`` would overwrite data that is not fixture data."""


@dataclass(frozen=True)
class FixturePlan:
    """Everything ``load`` writes, parsed and validated before connecting."""

    olist: list[olist_load.PreparedFile]
    fx_rows: list[fx_rates.FxRate]
    fx_window: tuple[date, date, tuple[str, ...]]
    holiday_rows: list[holidays.Holiday]
    holiday_plan: list[tuple[str, int]]

    def expected_rows(self) -> dict[str, int]:
        """``{"schema.table": rows}`` for every data table the load writes (not ``_load_audit``)."""
        counts = {f"{OLIST_SCHEMA}.{p.spec.table}": p.rows for p in self.olist}
        counts[f"{ENRICHMENT_SCHEMA}.fx_rates"] = len(self.fx_rows)
        counts[f"{ENRICHMENT_SCHEMA}.public_holidays"] = len(self.holiday_rows)
        return counts


def prepare_fixtures() -> FixturePlan:
    """Parse and validate every fixture with the loaders' own functions (no database access)."""
    prepared = olist_load.prepare(FIXTURES / "raw_olist")

    # parse_float=Decimal, as the loaders' HTTP client does (rates stay exact).
    fx_path = FIXTURES / "raw_enrichment" / "fx_rates_frankfurter_v1.json"
    fx = json.loads(fx_path.read_text(encoding="utf-8"), parse_float=Decimal)
    start = date.fromisoformat(fx["request"]["start"])
    end = date.fromisoformat(fx["request"]["end"])
    quotes = tuple(fx["request"]["quotes"])
    rates = fx_rates.parse_rates(fx["response"], start, end, quotes)

    payloads = json.loads(
        (FIXTURES / "raw_enrichment" / "public_holidays_nager_v3.json").read_text(encoding="utf-8")
    )
    plan: list[tuple[str, int]] = []
    rows: list[holidays.Holiday] = []
    for key, payload in payloads.items():
        country, year = key.split(":")
        plan.append((country, int(year)))
        rows.extend(holidays.parse_holidays(payload, country, int(year)))
    return FixturePlan(prepared, rates, (start, end, quotes), rows, plan)


def refuse_if_not_fixture_db(cur, expected: dict[str, int]) -> None:
    """Raise ``RefusedError`` if a table ``load`` writes already has more rows than the fixtures."""
    from psycopg import sql

    too_big: list[str] = []
    for name, rows in sorted(expected.items()):
        schema, table = name.split(".", 1)
        cur.execute("SELECT to_regclass(%s)", (sql.Identifier(schema, table).as_string(cur),))
        if cur.fetchone()[0] is None:
            continue
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, table)))
        existing = cur.fetchone()[0]
        if existing > rows:
            too_big.append(f"{name} has {existing} rows (fixtures: {rows})")
    if too_big:
        raise RefusedError(
            "this is not a fixture database, refusing to overwrite it: " + "; ".join(too_big)
        )


def load_fixtures(conn, plan: FixturePlan) -> None:
    """Load the plan with the loaders' own functions (each load is its own transaction)."""
    olist_load.load(conn, plan.olist)
    logger.info(
        "%s: %d tables, %d rows", OLIST_SCHEMA, len(plan.olist), sum(p.rows for p in plan.olist)
    )
    start, end, quotes = plan.fx_window
    n_rates = fx_rates.load_rates(conn, plan.fx_rows, start, end, quotes)
    logger.info("%s.fx_rates: %d rows", ENRICHMENT_SCHEMA, n_rates)
    n_holidays = holidays.load_holidays(conn, plan.holiday_rows, plan.holiday_plan)
    logger.info("%s.public_holidays: %d rows", ENRICHMENT_SCHEMA, n_holidays)


def user_schemas(cur) -> set[str]:
    cur.execute(
        "SELECT nspname FROM pg_namespace"
        " WHERE nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
        " AND nspname NOT LIKE 'pg\\_temp\\_%' AND nspname NOT LIKE 'pg\\_toast\\_temp\\_%'"
    )
    return {row[0] for row in cur.fetchall()}


def _relations(cur) -> dict[str, dict[str, Any]]:
    from psycopg import sql

    cur.execute(
        "SELECT c.oid, n.nspname, c.relname, c.relkind::text,"
        " CASE WHEN c.relkind IN ('v', 'm') THEN pg_get_viewdef(c.oid)"
        "      WHEN c.relkind IN ('i', 'I') THEN pg_get_indexdef(c.oid) END"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE (n.nspname LIKE %s OR n.nspname = %s) AND c.relkind <> 't'"
        " ORDER BY 2, 3",
        INVENTORY_SCHEMAS,
    )
    relations: dict[str, dict[str, Any]] = {}
    for oid, schema, name, kind, definition in cur.fetchall():
        entry: dict[str, Any] = {"kind": kind}
        if definition is not None:
            entry["definition"] = definition
        if kind in ROW_KINDS:
            cur.execute(
                "SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute"
                " WHERE attrelid = %s AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
                (oid,),
            )
            entry["columns"] = [list(row) for row in cur.fetchall()]
            # Rows as their text form, sorted: independent of physical order, sensitive to any
            # changed value (an UPDATE that keeps the row count still changes the md5).
            cur.execute(
                sql.SQL(
                    "SELECT count(*), md5(coalesce(string_agg(t::text, E'\\n'"
                    " ORDER BY t::text COLLATE \"C\"), '')) FROM {} AS t"
                ).format(sql.Identifier(schema, name))
            )
            entry["rows"], entry["md5"] = cur.fetchone()
        relations[f"{schema}.{name}"] = entry
    return relations


def _constraints_and_triggers(cur) -> dict[str, str]:
    cur.execute(
        "SELECT n.nspname || '.' || c.relname || ' constraint ' || k.conname,"
        " pg_get_constraintdef(k.oid)"
        " FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE (n.nspname LIKE %s OR n.nspname = %s)"
        " UNION ALL"
        " SELECT n.nspname || '.' || c.relname || ' trigger ' || g.tgname,"
        " pg_get_triggerdef(g.oid)"
        " FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE (n.nspname LIKE %s OR n.nspname = %s) AND NOT g.tgisinternal",
        INVENTORY_SCHEMAS * 2,
    )
    return dict(sorted(cur.fetchall()))


def _functions(cur) -> dict[str, str]:
    cur.execute(
        "SELECT n.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid)"
        " || ')', p.prokind::text || ' ' || coalesce(pg_get_function_result(p.oid), '')"
        " || ' md5:' || md5(coalesce(p.prosrc, ''))"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE (n.nspname LIKE %s OR n.nspname = %s)",
        INVENTORY_SCHEMAS,
    )
    return dict(sorted(cur.fetchall()))


def _types(cur) -> dict[str, str]:
    """Standalone types (enum, domain, range, base). Row types of relations and arrays are not."""
    cur.execute(
        "SELECT n.nspname || '.' || t.typname, t.typtype::text || ' ' || format_type(t.oid, NULL)"
        " || coalesce(' ' || format_type(t.typbasetype, t.typtypmod), '')"
        " FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
        " WHERE (n.nspname LIKE %s OR n.nspname = %s) AND t.typrelid = 0"
        " AND t.typcategory <> 'A'",
        INVENTORY_SCHEMAS,
    )
    return dict(sorted(cur.fetchall()))


def inventory(cur) -> dict[str, dict[str, Any]]:
    """Everything in the ``raw_*`` schemas and ``public`` that dbt must not change (module doc)."""
    return {
        "relations": _relations(cur),
        "constraints_triggers": _constraints_and_triggers(cur),
        "functions": _functions(cur),
        "types": _types(cur),
    }


def _diff_relation(name: str, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if before.get("kind") != after.get("kind"):
        problems.append(f"{name}: kind {before.get('kind')} -> {after.get('kind')}")
    if before.get("columns") != after.get("columns"):
        problems.append(f"{name}: columns {before.get('columns')} -> {after.get('columns')}")
    if before.get("rows") != after.get("rows"):
        problems.append(f"{name}: {before.get('rows')} rows before dbt, {after.get('rows')} after")
    elif before.get("md5") != after.get("md5"):
        problems.append(f"{name}: same row count but row contents changed (md5)")
    if before.get("definition") != after.get("definition"):
        problems.append(f"{name}: definition changed")
    return problems


def compare(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> list[str]:
    """Every difference between two inventories, one message each."""
    problems: list[str] = []
    for section in sorted(before.keys() | after.keys()):
        old, new = before.get(section, {}), after.get(section, {})
        for name in sorted(old.keys() | new.keys()):
            if name not in new:
                problems.append(f"{section}: {name} disappeared")
            elif name not in old:
                problems.append(f"{section}: {name} was created")
            elif section == "relations":
                problems.extend(_diff_relation(name, old[name], new[name]))
            elif old[name] != new[name]:
                problems.append(f"{section}: {name} changed")
    return problems


def check(cur, before: dict[str, dict[str, Any]]) -> list[str]:
    """Return the dbt schemas found; raise ``CheckError`` listing every violation."""
    problems: list[str] = []
    raw_before = {name.split(".", 1)[0] for name in before.get("relations", {})}
    schemas = user_schemas(cur)
    unexpected = schemas - BASE_SCHEMAS - DBT_SCHEMAS - raw_before
    if unexpected:
        problems.append(f"unexpected schemas: {sorted(unexpected)}")
    if "staging" not in schemas:
        problems.append("schema staging does not exist: did dbt build run?")
    problems.extend(compare(before, inventory(cur)))
    if problems:
        raise CheckError("; ".join(problems))
    return sorted(schemas & DBT_SCHEMAS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m warehouse.ci.fixture_db",
        description="Load warehouse fixtures into a throwaway DB, or check it after dbt build.",
    )
    parser.add_argument("command", choices=["load", "check"])
    parser.add_argument(
        "--inventory", type=Path, required=True, help="raw/public inventory (written by load)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    if args.command == "load" and os.environ.get(ALLOW_ENV, "").strip() != "1":
        logger.error(
            "refusing to load: set %s=1 only for a throwaway database (load replaces raw tables)",
            ALLOW_ENV,
        )
        return EXIT_CONFIG
    try:
        url = database_url_from_env()
    except ConfigError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    if args.command == "check" and not args.inventory.is_file():
        logger.error("inventory %s not found: run `load` first", args.inventory)
        return EXIT_CONFIG

    import psycopg

    try:
        plan = prepare_fixtures() if args.command == "load" else None
    except (olist_load.LoadError, FetchError) as exc:
        logger.error("fixtures are invalid: %s", exc)
        return EXIT_FAILED
    try:
        conn = connect(url)
    except psycopg.Error as exc:
        logger.error("could not connect (%s); check DATABASE_URL", type(exc).__name__)
        return EXIT_FAILED
    try:
        with conn:
            if plan is not None:
                with conn.cursor() as cur:
                    refuse_if_not_fixture_db(cur, plan.expected_rows())
                conn.commit()
                load_fixtures(conn, plan)
                with conn.cursor() as cur:
                    found = inventory(cur)
                args.inventory.write_text(json.dumps(found, indent=2, sort_keys=True) + "\n")
                logger.info(
                    "inventory of %d relations written to %s",
                    len(found["relations"]),
                    args.inventory,
                )
            else:
                before = json.loads(args.inventory.read_text(encoding="utf-8"))
                with conn.cursor() as cur:
                    schemas = check(cur, before)
                logger.info(
                    "ok: dbt schemas %s; %d raw/public relations unchanged",
                    schemas,
                    len(before.get("relations", {})),
                )
    except RefusedError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    except (olist_load.LoadError, FetchError, SchemaDriftError, CheckError, psycopg.Error) as exc:
        logger.error("%s failed: %s", args.command, exc)
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
