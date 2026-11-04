"""Prepare a throwaway PostgreSQL for the CI ``dbt build`` and check what dbt did to it (ADR-016).

    DATABASE_URL=postgresql://... python -m warehouse.ci.fixture_db load  --inventory <file>
    dbt build --project-dir warehouse --profiles-dir warehouse
    DATABASE_URL=postgresql://... python -m warehouse.ci.fixture_db check --inventory <file>

``load`` fills ``raw_olist`` and ``raw_enrichment`` from ``warehouse/fixtures/`` with the **real
loaders** (``ingestion.olist.load``, ``ingestion.enrichment.fx_rates`` / ``holidays``), so the raw
tables have exactly the loaders' DDL and the fixtures pass the loaders' own validation. dbt seeds
are not used: dbt never writes into a ``raw_*`` schema. It then writes an inventory of every
relation in every ``raw_*`` schema with its row count.

``check`` runs after ``dbt build`` and fails if dbt created any schema other than ``staging``,
``core`` and ``marts``, or changed the ``raw_*`` schemas (a relation added, dropped or with a
different row count than in the inventory).

Never point this at a real warehouse: ``load`` replaces the raw tables' contents. ``DATABASE_URL``
is never logged. Exit codes: 0 ok · 1 load or check failed · 2 configuration error.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from ingestion.db import ConfigError, connect, database_url_from_env
from ingestion.enrichment import fx_rates, holidays
from ingestion.enrichment.http import FetchError
from ingestion.enrichment.store import SchemaDriftError
from ingestion.olist import load as olist_load

EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
DBT_SCHEMAS = frozenset({"staging", "core", "marts"})
# Schemas that exist before dbt runs and that dbt must leave alone (besides raw_*).
BASE_SCHEMAS = frozenset({"public"})

logger = logging.getLogger(__name__)


class CheckError(RuntimeError):
    """dbt created an unexpected schema or touched a raw schema."""


def load_fixtures(conn) -> None:
    """Load every fixture with the loaders' own functions (each load is its own transaction)."""
    prepared = olist_load.prepare(FIXTURES / "raw_olist")
    olist_load.load(conn, prepared)
    logger.info("raw_olist: %d tables, %d rows", len(prepared), sum(p.rows for p in prepared))

    # parse_float=Decimal, as the loaders' HTTP client does (rates stay exact).
    fx_path = FIXTURES / "raw_enrichment" / "fx_rates_frankfurter_v1.json"
    fx = json.loads(fx_path.read_text(encoding="utf-8"), parse_float=Decimal)
    start = date.fromisoformat(fx["request"]["start"])
    end = date.fromisoformat(fx["request"]["end"])
    quotes = tuple(fx["request"]["quotes"])
    rates = fx_rates.parse_rates(fx["response"], start, end, quotes)
    n_rates = fx_rates.load_rates(conn, rates, start, end, quotes)
    logger.info("raw_enrichment.fx_rates: %d rows", n_rates)

    payloads = json.loads(
        (FIXTURES / "raw_enrichment" / "public_holidays_nager_v3.json").read_text(encoding="utf-8")
    )
    plan: list[tuple[str, int]] = []
    rows: list[holidays.Holiday] = []
    for key, payload in payloads.items():
        country, year = key.split(":")
        plan.append((country, int(year)))
        rows.extend(holidays.parse_holidays(payload, country, int(year)))
    n_holidays = holidays.load_holidays(conn, rows, plan)
    logger.info("raw_enrichment.public_holidays: %d rows", n_holidays)


def user_schemas(cur) -> set[str]:
    cur.execute(
        "SELECT nspname FROM pg_namespace"
        " WHERE nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
        " AND nspname NOT LIKE 'pg\\_temp\\_%' AND nspname NOT LIKE 'pg\\_toast\\_temp\\_%'"
    )
    return {row[0] for row in cur.fetchall()}


def raw_inventory(cur) -> dict[str, int]:
    """``{"schema.relation": row_count}`` for every table, view or matview in a ``raw_*`` schema."""
    from psycopg import sql

    cur.execute(
        "SELECT n.nspname, c.relname FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname LIKE 'raw\\_%' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')"
        " ORDER BY 1, 2"
    )
    inventory: dict[str, int] = {}
    for schema, relation in cur.fetchall():
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, relation)))
        inventory[f"{schema}.{relation}"] = cur.fetchone()[0]
    return inventory


def check(cur, before: dict[str, int]) -> list[str]:
    """Return the dbt schemas found; raise ``CheckError`` listing every violation."""
    problems: list[str] = []
    raw_before = {name.split(".", 1)[0] for name in before}
    schemas = user_schemas(cur)
    unexpected = schemas - BASE_SCHEMAS - DBT_SCHEMAS - raw_before
    if unexpected:
        problems.append(f"unexpected schemas: {sorted(unexpected)}")
    if "staging" not in schemas:
        problems.append("schema staging does not exist: did dbt build run?")
    after = raw_inventory(cur)
    for name in sorted(before.keys() | after.keys()):
        if name not in after:
            problems.append(f"{name} disappeared")
        elif name not in before:
            problems.append(f"{name} was created in a raw schema")
        elif before[name] != after[name]:
            problems.append(f"{name}: {before[name]} rows before dbt, {after[name]} after")
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
        "--inventory", type=Path, required=True, help="raw relation inventory (written by load)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
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
        conn = connect(url)
    except psycopg.Error as exc:
        logger.error("could not connect (%s); check DATABASE_URL", type(exc).__name__)
        return EXIT_FAILED
    try:
        with conn:
            if args.command == "load":
                load_fixtures(conn)
                with conn.cursor() as cur:
                    inventory = raw_inventory(cur)
                args.inventory.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
                logger.info(
                    "inventory of %d raw relations written to %s", len(inventory), args.inventory
                )
            else:
                before = json.loads(args.inventory.read_text(encoding="utf-8"))
                with conn.cursor() as cur:
                    found = check(cur, before)
                logger.info("ok: dbt schemas %s; %d raw relations unchanged", found, len(before))
    except (olist_load.LoadError, FetchError, SchemaDriftError, CheckError, psycopg.Error) as exc:
        logger.error("%s failed: %s", args.command, exc)
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
