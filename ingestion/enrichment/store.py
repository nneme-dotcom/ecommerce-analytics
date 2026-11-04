"""PostgreSQL side of the enrichment loads: schema ``raw_enrichment`` (DATA_CONTRACT §2b).

Tables are created on first use. If an existing table's columns (names, order or types) differ
from the definitions below, it is dropped and re-created inside the load transaction, as for
``raw_olist`` (no ``CASCADE``: a dependent view makes the load fail instead of vanishing).
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

INDEXES = (
    f"CREATE INDEX IF NOT EXISTS public_holidays_country_date"
    f" ON {SCHEMA}.public_holidays (country_code, holiday_date)",
)

logger = logging.getLogger(__name__)


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


def ensure_schema(cur) -> None:
    """Create (or re-create on drift) every table. Must run inside the load transaction."""
    # Serialise concurrent loads (CREATE ... IF NOT EXISTS is not race-free).
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('raw_enrichment'))")
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    for table, (columns, table_ddl) in TABLES.items():
        expected = [(name, typ) for name, typ, _ in columns]
        existing = _existing_columns(cur, table)
        if existing == expected:
            continue
        if existing:
            logger.info("re-creating %s.%s (columns changed)", SCHEMA, table)
            cur.execute(f"DROP TABLE {SCHEMA}.{table}")
        parts = [f"{name} {typ} {extra}".strip() for name, typ, extra in columns]
        if table_ddl:
            parts.append(table_ddl)
        cur.execute(f"CREATE TABLE {SCHEMA}.{table} ({', '.join(parts)})")
    for statement in INDEXES:
        cur.execute(statement)


def write_audit(cur, load_id: uuid.UUID, table: str, rows: int, params: dict[str, Any]) -> None:
    cur.execute(
        f"INSERT INTO {SCHEMA}._load_audit (load_id, table_name, row_count, params)"  # noqa: S608
        " VALUES (%s, %s, %s, %s::jsonb)",
        (load_id, table, rows, json.dumps(params, sort_keys=True)),
    )
