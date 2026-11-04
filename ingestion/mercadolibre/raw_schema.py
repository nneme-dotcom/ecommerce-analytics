"""PostgreSQL schema ``raw_mercadolibre`` (DATA_CONTRACT §1b): table definitions and drift handling.

Every data table here (``runs`` and the four snapshot-file tables) is owned by the ML raw loader. If
its columns (names, order or types) differ from the definitions below, it is dropped and re-created
inside the load transaction (no ``CASCADE``: a dependent view makes the load fail instead of
vanishing). ``_load_audit`` is append-only and never dropped: it is checked **before** any other
table is touched, and drift raises :class:`SchemaDriftError` (the transaction rolls back).

Three steps, so that the loader can read the stored dates before anything is dropped:
:func:`prepare_schema` (lock, schema, ``_load_audit``), :func:`stale_data_tables` (read-only) and
:func:`recreate_data_tables`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

SCHEMA = "raw_mercadolibre"
AUDIT_TABLE = "_load_audit"
RUNS_TABLE = "runs"
FILE_TABLES = ("highlights", "products", "offers", "trends")  # = snapshot.FILES


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # as printed by format_type()
    required: bool = False  # NOT NULL


def _cols(*specs: tuple[str, str] | tuple[str, str, bool]) -> tuple[Column, ...]:
    return tuple(Column(*s) for s in specs)


COMMON_HEAD = _cols(
    ("snapshot_date", "date", True),
    ("run_id", "uuid", True),
    ("line_no", "integer", True),
    ("captured_at", "timestamp with time zone", True),
)
PAYLOAD = Column("payload", "jsonb", True)

# Typed fields per file table: exactly the DATA_CONTRACT §1 fields, in that order.
FILE_FIELDS: dict[str, tuple[Column, ...]] = {
    "highlights": _cols(
        ("category_id", "text", True),
        ("position", "integer"),
        ("product_id", "text", True),
        ("type", "text"),
    ),
    "products": _cols(
        ("product_id", "text", True),
        ("name", "text"),
        ("domain_id", "text"),
        ("family_name", "text"),
        ("status", "text"),
        ("brand", "text"),
        ("model", "text"),
        ("permalink", "text"),
    ),
    "offers": _cols(
        ("product_id", "text", True),
        ("item_id", "text"),
        ("seller_id", "bigint"),
        ("official_store_id", "bigint"),
        ("price", "numeric"),
        ("original_price", "numeric"),
        ("currency_id", "text"),
        ("condition", "text"),
        ("listing_type_id", "text"),
        ("category_id", "text"),
        ("free_shipping", "boolean"),
        ("logistic_type", "text"),
        ("shipping_mode", "text"),
        ("deal_ids", "text[]"),
        ("tags", "text[]"),
        ("seller_state", "text"),
    ),
    "trends": _cols(
        ("position", "integer", True),
        ("keyword", "text"),
        ("url", "text"),
    ),
}

RUN_FIELDS = _cols(
    ("snapshot_date", "date", True),
    ("run_id", "uuid", True),
    ("status", "text", True),
    ("started_at", "timestamp with time zone"),
    ("finished_at", "timestamp with time zone"),
    ("site_id", "text"),
    ("categories", "text[]"),
    ("categories_requested", "text[]"),
    ("requests_made", "integer"),
    ("request_cap", "integer"),
    ("request_cap_reached", "boolean"),
    ("row_counts", "jsonb", True),
    ("skipped_highlights", "jsonb"),
    ("products_without_offers", "integer"),
    ("errors", "jsonb"),
)
RUN_LOADER_COLUMNS = _cols(
    ("payload", "jsonb", True),
    ("load_id", "uuid", True),
    ("file_sha256", "jsonb", True),
)


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    table_ddl: str
    defaults: dict[str, str] | None = None

    def expected(self) -> list[tuple[str, str]]:
        return [(c.name, c.type) for c in self.columns]


TABLES: dict[str, Table] = {
    RUNS_TABLE: Table(RUNS_TABLE, RUN_FIELDS + RUN_LOADER_COLUMNS, "PRIMARY KEY (snapshot_date)"),
    **{
        name: Table(
            name,
            COMMON_HEAD + FILE_FIELDS[name] + (PAYLOAD,),
            "PRIMARY KEY (snapshot_date, line_no)",
        )
        for name in FILE_TABLES
    },
    AUDIT_TABLE: Table(
        AUDIT_TABLE,
        _cols(
            ("load_id", "uuid", True),
            ("snapshot_date", "date", True),
            ("table_name", "text", True),
            ("run_id", "uuid", True),
            ("source_file", "text", True),
            ("row_count", "bigint", True),
            ("file_sha256", "character(64)", True),
            ("loaded_at", "timestamp with time zone", True),
        ),
        "PRIMARY KEY (load_id, snapshot_date, table_name)",
        {"loaded_at": "now()"},
    ),
}
DATA_TABLES = (RUNS_TABLE, *FILE_TABLES)

logger = logging.getLogger(__name__)


class SchemaDriftError(Exception):
    """``_load_audit`` has columns that differ from the contract (it is never re-created)."""


def existing_columns(cur, table: str) -> list[tuple[str, str]]:
    """``(name, format_type)`` of the columns of ``raw_mercadolibre.<table>`` ([] if missing)."""
    cur.execute(
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod)"
        " FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped"
        " ORDER BY a.attnum",
        (SCHEMA, table),
    )
    return [(name, typ) for name, typ in cur.fetchall()]


def _create_table(cur, table: Table) -> None:
    from psycopg import sql

    parts = []
    for col in table.columns:
        ddl = sql.SQL("{} {}").format(sql.Identifier(col.name), sql.SQL(col.type))
        if col.required:
            ddl = sql.SQL("{} NOT NULL").format(ddl)
        default = (table.defaults or {}).get(col.name)
        if default:
            ddl = sql.SQL("{} DEFAULT {}").format(ddl, sql.SQL(default))
        parts.append(ddl)
    parts.append(sql.SQL(table.table_ddl))
    cur.execute(
        sql.SQL("CREATE TABLE {} ({})").format(
            sql.Identifier(SCHEMA, table.name), sql.SQL(", ").join(parts)
        )
    )


def prepare_schema(cur) -> None:
    """Lock, create the schema and check/create ``_load_audit`` (first, before any other table).

    Raises :class:`SchemaDriftError` if ``_load_audit`` drifted.
    """
    from psycopg import sql

    # Serialise concurrent loads (CREATE ... IF NOT EXISTS is not race-free).
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (SCHEMA,))
    cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA)))

    audit = TABLES[AUDIT_TABLE]
    existing = existing_columns(cur, AUDIT_TABLE)
    if not existing:
        _create_table(cur, audit)
    elif existing != audit.expected():
        raise SchemaDriftError(
            f"{SCHEMA}.{AUDIT_TABLE} columns differ from DATA_CONTRACT §1b; it is append-only"
            " and never re-created: migrate it by hand"
        )


def stale_data_tables(cur) -> dict[str, bool]:
    """Data tables whose columns differ from the contract -> ``True`` if the table exists (drift,
    to be dropped) or ``False`` if it is missing (to be created). Read-only."""
    stale: dict[str, bool] = {}
    for name in DATA_TABLES:
        existing = existing_columns(cur, name)
        if existing != TABLES[name].expected():
            stale[name] = bool(existing)
    return stale


def recreate_data_tables(cur, stale: dict[str, bool]) -> set[str]:
    """Drop (no ``CASCADE``) and create the tables returned by :func:`stale_data_tables`."""
    from psycopg import sql

    for name in DATA_TABLES:
        if name not in stale:
            continue
        if stale[name]:
            logger.info("re-creating %s.%s (columns changed)", SCHEMA, name)
            cur.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(SCHEMA, name)))
        _create_table(cur, TABLES[name])
    return set(stale)
