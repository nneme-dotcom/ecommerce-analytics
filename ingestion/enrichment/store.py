"""PostgreSQL side of the enrichment loads: schema ``raw_enrichment`` (DATA_CONTRACT §2b).

Tables are created on first use. If the columns (names, order or types) of the table a load
writes (its *target*) differ from the definitions below, that table alone is dropped and
re-created inside the load transaction, as for ``raw_olist`` (no ``CASCADE``: a dependent view
makes the load fail instead of vanishing). Other data tables are only created if missing; a
drifted one is left untouched (with a warning) for its own loader to fix. ``_load_audit`` is
append-only and never dropped: if its columns drift, :class:`SchemaDriftError` aborts the load
and the transaction rolls back with nothing changed.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

SCHEMA = "raw_enrichment"

# table -> ((column, type as printed by format_type, extra DDL), ...), table-level DDL
TABLES: dict[str, tuple[tuple[tuple[str, str, str], ...], str]] = {
    "fx_rates": (
        (
            ("rate_date", "date", "NOT NULL"),
            ("base_currency", "character(3)", "NOT NULL"),
            ("quote_currency", "character(3)", "NOT NULL"),
            ("rate", "numeric", "NOT NULL CHECK (rate > 0)"),
            ("source", "text", "NOT NULL"),
        ),
        "PRIMARY KEY (rate_date, base_currency, quote_currency)",
    ),
    "public_holidays": (
        (
            ("country_code", "character(2)", "NOT NULL"),
            ("holiday_date", "date", "NOT NULL"),
            ("local_name", "text", ""),
            ("name", "text", "NOT NULL"),
            ("is_global", "boolean", "NOT NULL"),
            ("counties", "text[]", ""),
            ("holiday_types", "text[]", "NOT NULL"),
            ("is_fixed", "boolean", ""),
            ("launch_year", "integer", ""),
            ("source", "text", "NOT NULL"),
        ),
        "",
    ),
    "_load_audit": (
        (
            ("load_id", "uuid", "NOT NULL"),
            ("table_name", "text", "NOT NULL"),
            ("row_count", "bigint", "NOT NULL"),
            ("params", "jsonb", "NOT NULL"),
            ("loaded_at", "timestamp with time zone", "NOT NULL DEFAULT now()"),
        ),
        "PRIMARY KEY (load_id, table_name)",
    ),
}

AUDIT_TABLE = "_load_audit"

# table -> index statements (run only when the table matches its definition)
INDEXES: dict[str, tuple[str, ...]] = {
    "public_holidays": (
        f"CREATE INDEX IF NOT EXISTS public_holidays_country_date"
        f" ON {SCHEMA}.public_holidays (country_code, holiday_date)",
    ),
}

logger = logging.getLogger(__name__)


class SchemaDriftError(Exception):
    """A table that must not be re-created has columns that differ from the contract."""


def _existing_columns(cur, table: str) -> list[tuple[str, str]]:
    cur.execute(
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod)"
        " FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped"
        " ORDER BY a.attnum",
        (SCHEMA, table),
    )
    return [(name, typ) for name, typ in cur.fetchall()]


def _create_table(cur, table: str) -> None:
    columns, table_ddl = TABLES[table]
    parts = [f"{name} {typ} {extra}".strip() for name, typ, extra in columns]
    if table_ddl:
        parts.append(table_ddl)
    cur.execute(f"CREATE TABLE {SCHEMA}.{table} ({', '.join(parts)})")


def ensure_schema(cur, target: str) -> None:
    """Prepare the schema for a load into ``target``. Must run inside the load transaction.

    Every missing table is created. On column drift only ``target`` is dropped and re-created;
    a drifted ``_load_audit`` raises :class:`SchemaDriftError` (never dropped: append-only);
    any other drifted table is left as is.
    """
    if target not in TABLES or target == AUDIT_TABLE:
        raise ValueError(f"not a load target: {target!r}")
    # Serialise concurrent loads (CREATE ... IF NOT EXISTS is not race-free).
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('raw_enrichment'))")
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    for table, (columns, _) in TABLES.items():
        expected = [(name, typ) for name, typ, _ in columns]
        existing = _existing_columns(cur, table)
        if existing == expected:
            pass
        elif not existing:
            _create_table(cur, table)
        elif table == AUDIT_TABLE:
            raise SchemaDriftError(
                f"{SCHEMA}.{AUDIT_TABLE} columns differ from DATA_CONTRACT §2b; it is append-only"
                " and never re-created: migrate it by hand"
            )
        elif table == target:
            logger.info("re-creating %s.%s (columns changed)", SCHEMA, table)
            cur.execute(f"DROP TABLE {SCHEMA}.{table}")
            _create_table(cur, table)
        else:
            logger.warning(
                "%s.%s columns differ from the contract; left as is (its own loader re-creates it)",
                SCHEMA,
                table,
            )
            continue
        for statement in INDEXES.get(table, ()):
            cur.execute(statement)


def write_audit(cur, load_id: uuid.UUID, table: str, rows: int, params: dict[str, Any]) -> None:
    cur.execute(
        f"INSERT INTO {SCHEMA}._load_audit (load_id, table_name, row_count, params)"  # noqa: S608
        " VALUES (%s, %s, %s, %s::jsonb)",
        (load_id, table, rows, json.dumps(params, sort_keys=True)),
    )
