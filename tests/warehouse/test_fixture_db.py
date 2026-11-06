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


def test_load_refuses_more_ml_offers_than_the_fixtures(loaded, clean_db, tmp_path):
    # A "real" ML load: one more offer row than the fixtures (copy under a new line number).
    clean_db.execute(
        "INSERT INTO raw_mercadolibre.offers SELECT snapshot_date, run_id, line_no + 1000,"
        " captured_at, product_id, item_id, seller_id, official_store_id, price, original_price,"
        " currency_id, condition, listing_type_id, category_id, free_shipping, logistic_type,"
        " shipping_mode, deal_ids, tags, seller_state, payload"
        " FROM raw_mercadolibre.offers LIMIT 1"
    )
    offers = _count(clean_db, "raw_mercadolibre.offers")
    assert main(["load", "--inventory", str(tmp_path / "again.json")]) == EXIT_CONFIG
    assert _count(clean_db, "raw_mercadolibre.offers") == offers
    assert not (tmp_path / "again.json").exists()


def test_ml_fixture_days_go_through_the_real_loader(loaded, clean_db):
    expected = fixture_db.prepare_fixtures().expected_rows()
    for table in ("runs", "highlights", "products", "offers", "trends"):
        name = f"raw_mercadolibre.{table}"
        assert _count(clean_db, name) == expected[name]
    statuses = clean_db.execute(
        "SELECT snapshot_date::text, status FROM raw_mercadolibre.runs ORDER BY 1"
    ).fetchall()
    assert statuses == [
        ("2026-11-03", "success"),
        ("2026-11-04", "partial"),
        ("2026-11-05", "failed"),
    ]
    # One audit row per table per day, written by ingestion.mercadolibre.load.
    assert _count(clean_db, "raw_mercadolibre._load_audit") == 5 * len(statuses)
    # The edge cases the ML models rely on are in the data (ADR-019, fixtures README).
    one = clean_db.execute(
        "SELECT"
        " count(*) FILTER (WHERE original_price > price),"
        " count(*) FILTER (WHERE original_price <= price),"
        " count(*) FILTER (WHERE original_price IS NULL),"
        " count(DISTINCT item_id) FILTER (WHERE item_id IN (SELECT item_id"
        "   FROM raw_mercadolibre.offers GROUP BY 1 HAVING count(DISTINCT snapshot_date) > 1))"
        " FROM raw_mercadolibre.offers"
    ).fetchone()
    assert all(n > 0 for n in one), one
    no_offers = clean_db.execute(
        "SELECT count(*) FROM raw_mercadolibre.products p WHERE NOT EXISTS (SELECT 1"
        " FROM raw_mercadolibre.offers o WHERE o.snapshot_date = p.snapshot_date"
        " AND o.product_id = p.product_id)"
    ).fetchone()[0]
    assert no_offers > 0
    # The malformed offers behind the fixture build's expected WARN=4 (fixtures README): one
    # per column the fact requires, all on the partial day, each missing only that column.
    malformed = clean_db.execute(
        "SELECT"
        " count(*) FILTER (WHERE item_id IS NULL),"
        " count(*) FILTER (WHERE seller_id IS NULL),"
        " count(*) FILTER (WHERE price IS NULL),"
        " count(*) FILTER (WHERE currency_id IS NULL),"
        " count(*) FILTER (WHERE item_id IS NULL OR seller_id IS NULL OR price IS NULL"
        "   OR currency_id IS NULL),"
        " count(DISTINCT snapshot_date) FILTER (WHERE item_id IS NULL OR seller_id IS NULL"
        "   OR price IS NULL OR currency_id IS NULL)"
        " FROM raw_mercadolibre.offers"
    ).fetchone()
    assert malformed == (1, 1, 1, 1, 4, 1), malformed


def test_ml_fixture_days_are_reproducible(tmp_path):
    from warehouse.ci import ml_fixture_days

    first, second = tmp_path / "a", tmp_path / "b"
    ml_fixture_days.write_days(first)
    ml_fixture_days.write_days(second)
    files = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
    assert len(files) == 3 * 5  # three days x (run.json + four .jsonl.gz)
    for rel in files:
        assert (first / rel).read_bytes() == (second / rel).read_bytes(), rel


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
    # The fixture orders grow with each modelling task (ADR-016); count them from the plan.
    expected = fixture_db.prepare_fixtures().expected_rows()["raw_olist.orders"]
    assert expected >= 3
    assert orders["rows"] == expected
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
        (
            "UPDATE raw_mercadolibre.offers SET price = price + 1 WHERE line_no = 1",
            "raw_mercadolibre.offers: same row count but row contents changed",
        ),
        (
            "CREATE VIEW raw_mercadolibre.v AS SELECT 1 AS x",
            "relations: raw_mercadolibre.v was created",
        ),
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
