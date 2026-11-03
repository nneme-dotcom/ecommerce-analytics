import csv
import json
import shutil
from pathlib import Path

import pytest

from ingestion.olist.download import sha256_file
from ingestion.olist.files import MANIFEST_NAME, OLIST_FILES
from ingestion.olist.load import LoadError, count_records, load, main, prepare, read_header

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def source(tmp_path) -> Path:
    """A copy of the fixture CSVs with a manifest, as the download step leaves them."""
    folder = tmp_path / "olist"
    folder.mkdir()
    files = {}
    for spec in OLIST_FILES:
        shutil.copyfile(FIXTURES / spec.filename, folder / spec.filename)
        files[spec.filename] = {"sha256": sha256_file(folder / spec.filename), "bytes": 0}
    (folder / MANIFEST_NAME).write_text(json.dumps({"files": files}))
    return folder


def csv_records(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return sum(1 for _ in csv.reader(fh)) - 1


# --- file checks (no database) --------------------------------------------------------------


def test_counts_records_not_lines():
    reviews = FIXTURES / "olist_order_reviews_dataset.csv"
    assert count_records(reviews, 7) == 3
    assert len(reviews.read_bytes().splitlines()) > 4  # wc -l would over-count


def test_header_tolerates_bom():
    header = read_header(FIXTURES / "product_category_name_translation.csv")
    assert header == ["product_category_name", "product_category_name_english"]


def test_prepare_counts_every_file(source):
    prepared = prepare(source)
    assert [p.spec.table for p in prepared] == [s.table for s in OLIST_FILES]
    for p in prepared:
        assert p.rows == csv_records(p.path)
        assert p.sha256 == sha256_file(p.path)


def test_prepare_rejects_header_drift(source):
    path = source / "olist_sellers_dataset.csv"
    path.write_text(path.read_text().replace("seller_city", "seller_town"))
    (source / MANIFEST_NAME).unlink()
    with pytest.raises(LoadError, match="seller_town"):
        prepare(source)


def test_prepare_rejects_checksum_mismatch(source):
    with (source / "olist_orders_dataset.csv").open("a") as fh:
        fh.write('"o9","c9","created","2018-01-01 00:00:00",,,,"2018-01-10 00:00:00"\n')
    with pytest.raises(LoadError, match="SHA-256"):
        prepare(source)


def test_prepare_rejects_wrong_arity_and_missing_files(source):
    (source / MANIFEST_NAME).unlink()
    with (source / "olist_sellers_dataset.csv").open("a") as fh:
        fh.write('"s9","01000","sao paulo"\n')
    with pytest.raises(LoadError, match="3 fields, expected 4"):
        prepare(source)
    (source / "olist_sellers_dataset.csv").unlink()
    with pytest.raises(LoadError, match="missing files"):
        prepare(source)


def test_prepare_without_manifest_warns(source, caplog):
    (source / MANIFEST_NAME).unlink()
    assert len(prepare(source)) == 9
    assert "checksums are not verified" in caplog.text


def test_main_without_database_url_is_config_error(source, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["--source", str(source)]) == 2


# --- database (throwaway PostgreSQL) --------------------------------------------------------


def table_counts(conn) -> dict[str, int]:
    out = {}
    with conn.cursor() as cur:
        for spec in OLIST_FILES:
            cur.execute(f'SELECT count(*) FROM raw_olist."{spec.table}"')  # noqa: S608
            out[spec.table] = cur.fetchone()[0]
    return out


def test_load_row_counts_match_csv_records(db_conn, source):
    load(db_conn, prepare(source))
    expected = {s.table: csv_records(source / s.filename) for s in OLIST_FILES}
    assert table_counts(db_conn) == expected


def test_columns_are_text_and_named_as_the_contract(db_conn, source):
    load(db_conn, prepare(source))
    with db_conn.cursor() as cur:
        for spec in OLIST_FILES:
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'raw_olist' AND table_name = %s ORDER BY ordinal_position",
                (spec.table,),
            )
            rows = cur.fetchall()
            assert tuple(r[0] for r in rows) == spec.columns
            assert {r[1] for r in rows} == {"text"}


def test_values_are_loaded_unchanged(db_conn, source):
    load(db_conn, prepare(source))
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT review_comment_title, review_comment_message FROM raw_olist.order_reviews "
            "ORDER BY review_id"
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT order_approved_at FROM raw_olist.orders WHERE order_status = 'canceled'"
        )
        canceled = cur.fetchone()
        cur.execute("SELECT geolocation_city FROM raw_olist.geolocation ORDER BY 1")
        cities = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT product_category_name FROM raw_olist.product_category_name_translation")
        categories = {r[0] for r in cur.fetchall()}
    assert rows[0] == (None, "Chegou antes do prazo, recomendo!")  # unquoted empty -> NULL
    assert rows[1][1] == (
        "Produto veio com defeito.\r\nPedi troca e ninguém respondeu.\r\n\r\n"
        'Não compro mais, "nunca"'
    )
    assert rows[2] == ("", "")  # quoted empty string stays ''
    assert canceled == (None,)
    assert "são paulo" in cities
    assert categories == {"beleza_saude", "perfumaria", "cama_mesa_banho"}  # no BOM leaks in


def test_reload_is_idempotent_and_audited(db_conn, source):
    first = load(db_conn, prepare(source))
    counts = table_counts(db_conn)
    second = load(db_conn, prepare(source))
    assert table_counts(db_conn) == counts
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT load_id, table_name, source_file, row_count, file_sha256, loaded_at "
            "FROM raw_olist._load_audit"
        )
        audit = cur.fetchall()
    assert len(audit) == 18
    assert {r[0] for r in audit} == {first, second}
    latest = {r[1]: r for r in audit if r[0] == second}
    for spec in OLIST_FILES:
        row = latest[spec.table]
        assert row[2] == spec.filename
        assert row[3] == counts[spec.table]
        assert row[4] == sha256_file(source / spec.filename)
        assert row[5] is not None


def test_failed_load_rolls_back_everything(db_conn, source, monkeypatch):
    load(db_conn, prepare(source))
    before = table_counts(db_conn)
    prepared = prepare(source)
    # Corrupt the last file after validation: COPY of the 9th table fails mid-load.
    last = prepared[-1].path
    last.write_text("product_category_name,product_category_name_english\na,b,c\n")
    with pytest.raises(Exception, match="extra data"):
        load(db_conn, prepared)
    db_conn.rollback()
    assert table_counts(db_conn) == before
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(DISTINCT load_id) FROM raw_olist._load_audit")
        assert cur.fetchone()[0] == 1


def test_count_mismatch_aborts(db_conn, source):
    prepared = prepare(source)
    tampered = [
        p if p.spec.table != "sellers" else type(p)(p.spec, p.path, p.sha256, 99) for p in prepared
    ]
    with pytest.raises(LoadError, match="loaded 2 rows but the CSV has 99"):
        load(db_conn, tampered)
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('raw_olist.sellers')")
        assert cur.fetchone()[0] is None  # first load ever: nothing committed at all


def test_table_with_drifted_columns_is_recreated(db_conn, source):
    with db_conn.cursor() as cur:
        cur.execute("CREATE SCHEMA raw_olist")
        cur.execute("CREATE TABLE raw_olist.sellers (seller_id text, old_column text)")
    db_conn.commit()
    load(db_conn, prepare(source))
    with db_conn.cursor() as cur:
        cur.execute("SELECT * FROM raw_olist.sellers LIMIT 0")
        assert tuple(d.name for d in cur.description) == OLIST_FILES[7].columns


def test_main_end_to_end(database_url, db_conn, source, monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", database_url)
    assert main(["--source", str(source)]) == 0
    assert table_counts(db_conn)["orders"] == 3
    assert database_url not in caplog.text


def test_main_bad_connection_string_is_not_echoed(source, monkeypatch, caplog):
    secret_url = "postgresql://user:sup3r-secret@127.0.0.1:1/db?sslmode=bogus"
    monkeypatch.setenv("DATABASE_URL", secret_url)
    assert main(["--source", str(source)]) == 1
    assert "sup3r-secret" not in caplog.text
