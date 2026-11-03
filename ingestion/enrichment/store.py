"""PostgreSQL side of the enrichment loads: schema ``raw_enrichment`` (DATA_CONTRACT §2b)."""

from __future__ import annotations

import json
import uuid
from typing import Any

SCHEMA = "raw_enrichment"

DDL = (
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.fx_rates (
        rate_date date NOT NULL,
        base_currency char(3) NOT NULL,
        quote_currency char(3) NOT NULL,
        rate numeric NOT NULL CHECK (rate > 0),
        source text NOT NULL,
        PRIMARY KEY (rate_date, base_currency, quote_currency))""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.public_holidays (
        country_code char(2) NOT NULL,
        holiday_date date NOT NULL,
        name text NOT NULL,
        national_holiday boolean,
        subdivision_codes text[] NOT NULL,
        holiday_types text[] NOT NULL,
        source text NOT NULL)""",
    f"""CREATE INDEX IF NOT EXISTS public_holidays_country_date
        ON {SCHEMA}.public_holidays (country_code, holiday_date)""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}._load_audit (
        load_id uuid NOT NULL,
        table_name text NOT NULL,
        row_count bigint NOT NULL,
        params jsonb NOT NULL,
        loaded_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (load_id, table_name))""",
)


def ensure_schema(cur) -> None:
    # Serialise concurrent loads (CREATE ... IF NOT EXISTS is not race-free).
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('raw_enrichment'))")
    for statement in DDL:
        cur.execute(statement)


def write_audit(cur, load_id: uuid.UUID, table: str, rows: int, params: dict[str, Any]) -> None:
    cur.execute(
        f"INSERT INTO {SCHEMA}._load_audit (load_id, table_name, row_count, params)"  # noqa: S608
        " VALUES (%s, %s, %s, %s::jsonb)",
        (load_id, table, rows, json.dumps(params, sort_keys=True)),
    )
