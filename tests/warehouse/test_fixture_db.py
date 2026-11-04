"""``python -m warehouse.ci.fixture_db``: the load refusals and what ``check`` catches (ADR-016)."""

from __future__ import annotations

import json
import re

import pytest

from warehouse.ci import fixture_db
from warehouse.ci.fixture_db import EXIT_CONFIG, EXIT_FAILED, EXIT_OK, CheckError, main


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608 - test literal


@pytest.fixture
def loaded(clean_db, tmp_path, monkeypatch):
    """Fixtures loaded with the opt-in, ``staging`` present (as after dbt build); the inventory."""
    monkeypatch.setenv(fixture_db.ALLOW_ENV, "1")
    inventory = tmp_path / "inventory.json"
    assert main(["load", "--inventory", str(inventory)]) == EXIT_OK
    clean_db.execute("CREATE SCHEMA staging")
    return inventory


# --- load refusals ------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes"])
def test_load_refuses_without_explicit_opt_in(clean_db, tmp_path, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv(fixture_db.ALLOW_ENV, value)
    inventory = tmp_path / "inventory.json"
    assert main(["load", "--inventory", str(inventory)]) == EXIT_CONFIG
    assert not inventory.exists()
    schemas = {r[0] for r in clean_db.execute("SELECT nspname FROM pg_namespace").fetchall()}
    assert not {"raw_olist", "raw_enrichment"} & schemas


def test_load_refuses_a_database_with_more_orders_than_the_fixtures(loaded, clean_db, tmp_path):
    fixture_orders = _count(clean_db, "raw_olist.orders")
    # A "real" load: one more order than the fixtures (copy of a row under a new id).
    clean_db.execute(
        "INSERT INTO raw_olist.orders SELECT 'real-order', customer_id, order_status,"
        " order_purchase_timestamp, order_approved_at, order_delivered_carrier_date,"
        " order_delivered_customer_date, order_estimated_delivery_date"
        " FROM raw_olist.orders LIMIT 1"
    )
    audit_rows = _count(clean_db, "raw_olist._load_audit")

    assert main(["load", "--inventory", str(tmp_path / "again.json")]) == EXIT_CONFIG

    # Nothing was truncated or reloaded.
    assert _count(clean_db, "raw_olist.orders") == fixture_orders + 1
    assert clean_db.execute(
        "SELECT count(*) FROM raw_olist.orders WHERE order_id = 'real-order'"
    ).fetchone() == (1,)
    assert _count(clean_db, "raw_olist._load_audit") == audit_rows
    assert not (tmp_path / "again.json").exists()


def test_load_refuses_more_enrichment_rows_than_the_fixtures(loaded, clean_db, tmp_path):
    clean_db.execute(
        "INSERT INTO raw_enrichment.fx_rates SELECT rate_date - 1000, base_currency,"
        " quote_currency, rate, source FROM raw_enrichment.fx_rates LIMIT 1"
    )
    assert main(["load", "--inventory", str(tmp_path / "again.json")]) == EXIT_CONFIG


def test_load_is_idempotent_on_a_fixture_database(loaded, tmp_path):
    again = tmp_path / "again.json"
    assert main(["load", "--inventory", str(again)]) == EXIT_OK
    first, second = json.loads(loaded.read_text()), json.loads(again.read_text())
    assert first["relations"]["raw_olist.orders"] == second["relations"]["raw_olist.orders"]


# --- check --------------------------------------------------------------------------------------


def test_inventory_covers_columns_rows_and_md5(loaded):
    orders = json.loads(loaded.read_text())["relations"]["raw_olist.orders"]
    assert orders["kind"] == "r"
    assert orders["columns"][0] == ["order_id", "text"]
    assert orders["rows"] == 3
    assert len(orders["md5"]) == 32


def test_check_passes_when_nothing_changed(loaded):
    assert main(["check", "--inventory", str(loaded)]) == EXIT_OK


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "UPDATE raw_olist.orders SET order_status = 'shipped' WHERE order_status = 'canceled'",
            "raw_olist.orders: same row count but row contents changed",
        ),
        ("ALTER TABLE raw_olist.customers ADD COLUMN extra text", "raw_olist.customers: columns"),
        (
            "ALTER TABLE raw_enrichment.fx_rates ALTER COLUMN source TYPE varchar(100)",
            "raw_enrichment.fx_rates: columns",
        ),
        (
            "CREATE FUNCTION raw_olist.f() RETURNS int LANGUAGE sql AS 'select 1'",
            "functions: raw_olist.f() was created",
        ),
        ("CREATE TABLE public.leak (id int)", "relations: public.leak was created"),
        ("CREATE VIEW public.leak_v AS SELECT 1 AS x", "relations: public.leak_v was created"),
        ("CREATE VIEW raw_olist.v AS SELECT 1 AS x", "relations: raw_olist.v was created"),
        (
            "CREATE INDEX fx_rate_idx ON raw_enrichment.fx_rates (rate)",
            "relations: raw_enrichment.fx_rate_idx was created",
        ),
        (
            "ALTER TABLE raw_enrichment.fx_rates ADD CONSTRAINT small CHECK (rate < 1000)",
            "constraints_triggers: raw_enrichment.fx_rates constraint small was created",
        ),
        ("CREATE TYPE raw_olist.mood AS ENUM ('ok')", "types: raw_olist.mood was created"),
        ("DELETE FROM raw_enrichment.public_holidays", "1 rows before dbt, 0 after"),
        ("DROP TABLE raw_olist.sellers", "relations: raw_olist.sellers disappeared"),
        ("CREATE SCHEMA analytics", "unexpected schemas: ['analytics']"),
        ("DROP SCHEMA staging", "schema staging does not exist"),
    ],
)
def test_check_catches_every_change(loaded, clean_db, statement, message):
    if "public_holidays" in statement:
        # Keep a single holiday row so the message is predictable.
        clean_db.execute(
            "DELETE FROM raw_enrichment.public_holidays WHERE ctid <> "
            "(SELECT min(ctid) FROM raw_enrichment.public_holidays)"
        )
        before = fixture_db.inventory(clean_db.cursor())
    else:
        before = json.loads(loaded.read_text())
    clean_db.execute(statement)
    with pytest.raises(CheckError, match=re.escape(message)):
        fixture_db.check(clean_db.cursor(), before)


def test_check_exit_code_on_a_change(loaded, clean_db):
    clean_db.execute("CREATE TABLE public.leak (id int)")
    assert main(["check", "--inventory", str(loaded)]) == EXIT_FAILED
