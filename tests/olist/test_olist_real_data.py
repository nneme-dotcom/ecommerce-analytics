"""Acceptance test on the real Olist files (task 1.4). Skipped unless ``OLIST_SOURCE_DIR`` points at
the folder with the nine extracted CSVs (the real data is never committed, so CI skips this).

    OLIST_SOURCE_DIR=data/olist pytest tests/olist/test_olist_real_data.py
"""

import os
from pathlib import Path

import pytest

from ingestion.olist.load import load, prepare

# Data records per file, counted with a CSV parser on the official Kaggle archive
# (44,717,580 bytes) on 2026-11-03. Also in DATA_CONTRACT §2.
EXPECTED_ROWS = {
    "customers": 99_441,
    "geolocation": 1_000_163,
    "order_items": 112_650,
    "order_payments": 103_886,
    "order_reviews": 99_224,
    "orders": 99_441,
    "products": 32_951,
    "sellers": 3_095,
    "product_category_name_translation": 71,
}

SOURCE = os.environ.get("OLIST_SOURCE_DIR", "").strip()
pytestmark = pytest.mark.skipif(not SOURCE, reason="OLIST_SOURCE_DIR not set (real data)")


def test_real_files_validate_with_expected_record_counts():
    prepared = prepare(Path(SOURCE))
    assert {p.spec.table: p.rows for p in prepared} == EXPECTED_ROWS


def test_real_load_row_counts(db_conn):
    load(db_conn, prepare(Path(SOURCE)))
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (table_name) table_name, row_count FROM raw_olist._load_audit "
            "ORDER BY table_name, loaded_at DESC"
        )
        audited = dict(cur.fetchall())
        counts = {}
        for table in EXPECTED_ROWS:
            cur.execute(f'SELECT count(*) FROM raw_olist."{table}"')  # noqa: S608 - fixed names
            counts[table] = cur.fetchone()[0]
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'raw_olist' "
            "AND table_name = 'product_category_name_translation' ORDER BY ordinal_position"
        )
        translation_columns = [r[0] for r in cur.fetchall()]
    assert counts == EXPECTED_ROWS
    assert audited == EXPECTED_ROWS
    assert translation_columns == ["product_category_name", "product_category_name_english"]
