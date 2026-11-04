"""ML raw loader (task 2.5): synthetic day folders from the real snapshot writer + throwaway DB.

Folders are generated in each test from ``ml_fixtures`` payloads (shaped like docs/API_PROBE.md),
mapped and written with ``ingestion.mercadolibre.snapshot`` itself, so the loader is tested against
exactly what the job produces. No real Mercado Libre data is used (ADR-003).
"""

import gzip
import json
import logging
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import ml_fixtures as fx
import pytest

from ingestion.mercadolibre import load as ml_load
from ingestion.mercadolibre import snapshot
from ingestion.mercadolibre.load import LoadError, describe_db_error, load, prepare
from ingestion.mercadolibre.raw_schema import SchemaDriftError

DAY1, DAY2 = "2026-11-03", "2026-11-04"
PRODUCTS = ("MLA58353028", "MLA2")
SECRET_LOOKING = "do-not-echo-this-line-content"


def make_day(
    root: Path,
    day: str,
    *,
    n_offers: int = 3,
    seed: int = 0,
    status: str = "success",
    offer_overrides: dict | None = None,
) -> str:
    """Write one day folder with the snapshot writer. Returns its run_id."""
    run_id = str(uuid.uuid4())
    collected = snapshot.Collected(run_id=run_id, snapshot_date=day)
    ts = f"{day}T06:00:12Z"
    categories = []
    if status != "failed":
        collected.add(
            "trends", snapshot.map_trends(fx.trends(["iphone", "aire acondicionado"])), ts
        )
        hl = snapshot.map_highlights(
            "MLA1055", fx.highlights([*PRODUCTS, ("MLAU77", "USER_PRODUCT")])
        )
        collected.add("highlights", hl.rows, ts)
        categories = ["MLA1055"]
        for pid in PRODUCTS:
            collected.add("products", [snapshot.map_product(pid, fx.product(pid))], ts)
            offers = [fx.offer(seed + i, **(offer_overrides or {})) for i in range(n_offers)]
            collected.add("offers", [snapshot.map_offer(pid, o) for o in offers], ts)
    meta = {
        "run_id": run_id,
        "snapshot_date": day,
        "started_at": f"{day}T06:00:00Z",
        "finished_at": f"{day}T06:03:00Z",
        "site_id": "MLA",
        "categories": categories,
        "categories_requested": ["MLA1055"],
        "requests_made": 7,
        "request_cap": 1000,
        "request_cap_reached": False,
        "row_counts": {k: len(collected.rows[k]) for k in snapshot.FILES},
        "skipped_highlights": {"USER_PRODUCT": 1} if categories else {},
        "products_without_offers": 0,
        "errors": []
        if categories
        else [{"path": "/highlights/MLA/category/MLA1055", "status": 500, "count": 4}],
        "status": status,
    }
    assert snapshot.write_snapshot(root, day, meta, collected) is not None
    return run_id


def day_dir(root: Path, day: str) -> Path:
    return root / "mercadolibre" / f"snapshot_date={day}"


def read_lines(path: Path) -> list[str]:
    return gzip.decompress(path.read_bytes()).decode("utf-8").splitlines()


def write_lines(path: Path, lines: list[str]) -> None:
    path.write_bytes(gzip.compress("".join(f"{line}\n" for line in lines).encode("utf-8")))


def edit_run_json(root: Path, day: str, **changes) -> None:
    path = day_dir(root, day) / "run.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.update(changes)
    path.write_text(json.dumps(meta), encoding="utf-8")


def counts(conn) -> dict[str, list[tuple]]:
    """Per table: (snapshot_date, count, run_id) rows, as a cheap fingerprint of the content."""
    out = {}
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('raw_mercadolibre.runs')")
        if cur.fetchone() == (None,):
            return {}
        for table in ("runs", "highlights", "products", "offers", "trends"):
            cur.execute(
                f"SELECT snapshot_date, count(*), min(run_id::text)"  # noqa: S608
                f" FROM raw_mercadolibre.{table} GROUP BY 1 ORDER BY 1"
            )
            out[table] = cur.fetchall()
    return out


def full_content(conn) -> dict[str, list[tuple]]:
    out = {}
    with conn.cursor() as cur:
        for table in ("highlights", "products", "offers", "trends"):
            cur.execute(
                f"SELECT * FROM raw_mercadolibre.{table} ORDER BY snapshot_date, line_no"  # noqa: S608
            )
            out[table] = cur.fetchall()
        cur.execute(
            "SELECT snapshot_date, run_id, status, payload, file_sha256 FROM raw_mercadolibre.runs"
            " ORDER BY 1"
        )
        out["runs"] = cur.fetchall()
    return out


def audit_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_mercadolibre._load_audit")
        return cur.fetchone()[0]


def run_main(monkeypatch, database_url, root, *extra) -> int:
    monkeypatch.setenv("DATABASE_URL", database_url)
    return ml_load.main(["--source", str(root), *extra])


# --------------------------------------------------------------------------- validation (no DB)


def test_prepare_validates_every_day_folder(tmp_path):
    run1 = make_day(tmp_path, DAY1)
    make_day(tmp_path, DAY2, n_offers=2)
    days = prepare(tmp_path)
    assert [d.snapshot_date for d in days] == [date(2026, 11, 3), date(2026, 11, 4)]
    assert str(days[0].run_id) == run1
    assert days[0].row_counts == {"highlights": 3, "products": 2, "offers": 6, "trends": 2}
    assert days[1].row_counts["offers"] == 4
    assert set(days[0].sha256) == {
        "run.json",
        "highlights.jsonl.gz",
        "products.jsonl.gz",
        "offers.jsonl.gz",
        "trends.jsonl.gz",
    }


def test_missing_run_json_fails(tmp_path):
    make_day(tmp_path, DAY1)
    (day_dir(tmp_path, DAY1) / "run.json").unlink()
    with pytest.raises(LoadError, match="run.json is missing"):
        prepare(tmp_path)


def test_missing_data_file_fails(tmp_path):
    make_day(tmp_path, DAY1)
    (day_dir(tmp_path, DAY1) / "trends.jsonl.gz").unlink()
    with pytest.raises(LoadError, match="trends.jsonl.gz is missing"):
        prepare(tmp_path)


def test_count_mismatch_fails(tmp_path):
    make_day(tmp_path, DAY1)
    edit_run_json(
        tmp_path, DAY1, row_counts={"highlights": 3, "products": 2, "offers": 7, "trends": 2}
    )
    with pytest.raises(LoadError, match="offers.jsonl.gz: 6 lines but run.json row_counts says 7"):
        prepare(tmp_path)


@pytest.mark.parametrize(
    "change, message",
    [
        ({"run_id": "not-a-uuid"}, "run_id missing or not a uuid"),
        ({"snapshot_date": DAY2}, "snapshot_date does not match the folder"),
        ({"status": "great"}, "status is not one of"),
        ({"row_counts": {"highlights": 3}}, "row_counts must hold"),
        (
            {"row_counts": {"highlights": True, "products": 2, "offers": 6, "trends": 2}},
            "row_counts must hold",
        ),
    ],
)
def test_bad_run_json_fails(tmp_path, change, message):
    make_day(tmp_path, DAY1)
    edit_run_json(tmp_path, DAY1, **change)
    with pytest.raises(LoadError, match=message):
        prepare(tmp_path)


def test_run_json_not_json_fails(tmp_path):
    make_day(tmp_path, DAY1)
    (day_dir(tmp_path, DAY1) / "run.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(LoadError, match="run.json: blank line or not valid JSON"):
        prepare(tmp_path)


def test_malformed_line_fails_without_echoing_its_content(tmp_path):
    make_day(tmp_path, DAY1)
    path = day_dir(tmp_path, DAY1) / "offers.jsonl.gz"
    lines = read_lines(path)
    lines[3] = lines[3][:-1] + f',"x":"{SECRET_LOOKING}"'  # truncated object
    write_lines(path, lines)
    with pytest.raises(LoadError) as excinfo:
        prepare(tmp_path)
    assert "offers.jsonl.gz:4: blank line or not valid JSON" in str(excinfo.value)
    assert SECRET_LOOKING not in str(excinfo.value)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda o: o.update(snapshot_date=DAY2), "snapshot_date does not match the folder"),
        (lambda o: o.update(run_id=str(uuid.uuid4())), "run_id does not match run.json"),
        (lambda o: o.update(captured_at="2026-11-03T06:00:12"), "captured_at missing"),
        (lambda o: o.pop("captured_at"), "captured_at missing"),
        (lambda o: o.update(product_id=None), "required field product_id"),
        (lambda o: o.update(price=float("nan")), "not valid JSON"),
        (lambda o: o.update(item_id="a\x00b"), "NUL character"),
        (lambda o: o.update(item_id="a\udc80b"), "lone UTF-16 surrogate"),  # typed text column
        (lambda o: o.update(extra={"k": ["\ud800"]}), "lone UTF-16 surrogate"),  # payload only
        (lambda o: o.update({"\udfff": 1}), "lone UTF-16 surrogate"),  # in a key
    ],
)
def test_bad_line_fields_fail(tmp_path, mutate, message):
    make_day(tmp_path, DAY1)
    path = day_dir(tmp_path, DAY1) / "offers.jsonl.gz"
    lines = read_lines(path)
    obj = json.loads(lines[0])
    mutate(obj)
    lines[0] = json.dumps(obj)
    write_lines(path, lines)
    with pytest.raises(LoadError, match=message):
        prepare(tmp_path)


def test_blank_line_and_non_object_fail(tmp_path):
    make_day(tmp_path, DAY1)
    path = day_dir(tmp_path, DAY1) / "trends.jsonl.gz"
    lines = read_lines(path)
    write_lines(path, [lines[0], "", lines[1]])
    with pytest.raises(LoadError, match="trends.jsonl.gz:2: blank line"):
        prepare(tmp_path)
    write_lines(path, [lines[0], "[1, 2]"])
    with pytest.raises(LoadError, match="trends.jsonl.gz:2: not a JSON object"):
        prepare(tmp_path)


def test_not_gzip_or_truncated_gzip_fails(tmp_path):
    make_day(tmp_path, DAY1)
    path = day_dir(tmp_path, DAY1) / "products.jsonl.gz"
    good = path.read_bytes()
    path.write_bytes(gzip.decompress(good))  # plain JSONL, not gzip
    with pytest.raises(LoadError, match="products.jsonl.gz: not a valid gzip file"):
        prepare(tmp_path)
    path.write_bytes(good[: len(good) // 2])
    with pytest.raises(LoadError, match="products.jsonl.gz: not a valid gzip file"):
        prepare(tmp_path)
    path.write_bytes(b"")  # gzip.decompress(b"") is b"": must not pass as a 0-line file
    with pytest.raises(LoadError, match=r"products.jsonl.gz: not a valid gzip file \(empty"):
        prepare(tmp_path)


def test_empty_file_written_by_the_snapshot_job_is_valid_gzip(tmp_path):
    make_day(tmp_path, DAY1, status="failed")
    assert (day_dir(tmp_path, DAY1) / "offers.jsonl.gz").stat().st_size > 0
    assert prepare(tmp_path)[0].row_counts["offers"] == 0


def test_deeply_nested_line_is_invalid_json_not_a_recursion_error(tmp_path):
    deep = "[" * 200_000 + "]" * 200_000
    with pytest.raises(LoadError, match="x:1: blank line or not valid JSON"):
        ml_load.parse_json(deep, "x:1", exact=True)
    make_day(tmp_path, DAY1)
    path = day_dir(tmp_path, DAY1) / "trends.jsonl.gz"
    lines = read_lines(path)
    lines[1] = '{"a":' + deep + "}"
    write_lines(path, lines)
    with pytest.raises(LoadError, match="trends.jsonl.gz:2: blank line or not valid JSON"):
        prepare(tmp_path)


def test_surrogate_pair_escape_is_valid():
    assert ml_load.parse_json('{"k": "\\ud83d\\ude00"}', "x", exact=True) == {"k": "\U0001f600"}


def test_folder_discovery(tmp_path, caplog):
    with pytest.raises(LoadError, match="no mercadolibre/ folder"):
        prepare(tmp_path)
    (tmp_path / "mercadolibre").mkdir()
    with pytest.raises(LoadError, match="no snapshot_date=YYYY-MM-DD folders"):
        prepare(tmp_path)
    make_day(tmp_path, DAY1)
    (tmp_path / "mercadolibre" / f".tmp-{DAY2}-abc").mkdir()
    (tmp_path / "mercadolibre" / f".old-{DAY2}-abc").mkdir()
    (tmp_path / "mercadolibre" / "notes.txt").write_text("x", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert [d.path.name for d in prepare(tmp_path)] == [f"snapshot_date={DAY1}"]
    assert "skipping leftover folder .tmp-" in caplog.text
    (tmp_path / "mercadolibre" / "snapshot_date=2026-13-01").mkdir()
    with pytest.raises(LoadError, match="folder name is not snapshot_date=YYYY-MM-DD"):
        prepare(tmp_path)


def test_main_without_database_url_is_config_error(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    make_day(tmp_path, DAY1)
    assert ml_load.main(["--source", str(tmp_path)]) == 2


def test_main_unexpected_validation_error_logs_only_its_type(tmp_path, monkeypatch, caplog):
    make_day(tmp_path, DAY1)

    def boom(source):
        raise OSError(f"cannot read {SECRET_LOOKING}")

    monkeypatch.setattr(ml_load, "prepare", boom)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw@127.0.0.1:1/x")
    with caplog.at_level(logging.INFO):
        assert ml_load.main(["--source", str(tmp_path)]) == 1
    assert "validation failed, nothing loaded: unexpected OSError" in caplog.text
    assert SECRET_LOOKING not in caplog.text


def test_main_bad_folder_fails_before_connecting(tmp_path, monkeypatch):
    make_day(tmp_path, DAY1)
    (day_dir(tmp_path, DAY1) / "run.json").unlink()

    def no_connect(url):
        raise AssertionError("must not connect when validation fails")

    monkeypatch.setattr(ml_load, "connect", no_connect)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw@127.0.0.1:1/x")
    assert ml_load.main(["--source", str(tmp_path)]) == 1


def test_describe_db_error_never_echoes_data_values():
    class FakeDiag:
        message_primary = f'invalid input syntax for type numeric: "{SECRET_LOOKING}"'

    class FakeDataError(Exception):
        sqlstate = "22P02"
        diag = FakeDiag()

    class FakeDependency(Exception):
        sqlstate = "2BP01"
        diag = type("D", (), {"message_primary": "cannot drop table offers"})()

    assert SECRET_LOOKING not in describe_db_error(FakeDataError("x"))
    assert "22P02" in describe_db_error(FakeDataError("x"))
    assert "cannot drop table offers" in describe_db_error(FakeDependency("x"))


# --------------------------------------------------------------------------- database


def test_load_typed_columns_and_payload(db_conn, tmp_path):
    run1 = make_day(tmp_path, DAY1)
    make_day(tmp_path, DAY2, n_offers=2, seed=10, offer_overrides={"official_store_id": 77})
    summary = load(db_conn, prepare(tmp_path))
    assert summary.loaded == [date(2026, 11, 3), date(2026, 11, 4)]
    assert summary.rows == {"highlights": 6, "products": 4, "offers": 10, "trends": 4}

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT run_id::text, line_no, product_id, item_id, seller_id, official_store_id,"
            " price, original_price, currency_id, condition, listing_type_id, category_id,"
            " free_shipping, logistic_type, shipping_mode, deal_ids, tags, seller_state,"
            " captured_at = '2026-11-03T06:00:12Z'::timestamptz, payload->>'item_id'"
            " FROM raw_mercadolibre.offers WHERE snapshot_date = %s ORDER BY line_no LIMIT 1",
            (DAY1,),
        )
        assert cur.fetchone() == (
            run1, 1, "MLA58353028", "MLA1000000000", 100, None,
            Decimal("255999.36"), Decimal("399999"), "ARS", "new", "gold_pro", "MLA1055",
            True, "cross_docking", "me2", ["DEAL1"], ["good_quality_thumbnail"],
            "Capital Federal", True, "MLA1000000000",
        )  # fmt: skip
        cur.execute(
            "SELECT DISTINCT official_store_id FROM raw_mercadolibre.offers WHERE snapshot_date"
            " = %s",
            (DAY2,),
        )
        assert cur.fetchall() == [(77,)]
        cur.execute(
            "SELECT category_id, position, product_id, type FROM raw_mercadolibre.highlights"
            " WHERE snapshot_date = %s ORDER BY line_no",
            (DAY1,),
        )
        assert cur.fetchall() == [
            ("MLA1055", 1, "MLA58353028", "PRODUCT"),
            ("MLA1055", 2, "MLA2", "PRODUCT"),
            ("MLA1055", 3, "MLAU77", "USER_PRODUCT"),
        ]
        cur.execute(
            "SELECT product_id, name, domain_id, family_name, brand, model"
            " FROM raw_mercadolibre.products WHERE snapshot_date = %s ORDER BY line_no",
            (DAY1,),
        )
        assert cur.fetchall()[0] == (
            "MLA58353028", "Product MLA58353028", "MLA-CELLPHONES", None, "Motorola", "Moto G06",
        )  # fmt: skip
        cur.execute(
            "SELECT position, keyword FROM raw_mercadolibre.trends WHERE snapshot_date = %s"
            " ORDER BY line_no",
            (DAY1,),
        )
        assert cur.fetchall() == [(1, "iphone"), (2, "aire acondicionado")]
        cur.execute(
            "SELECT run_id::text, status, site_id, categories, requests_made,"
            " request_cap_reached, row_counts, skipped_highlights, errors, payload->>'status',"
            " file_sha256 ? 'offers.jsonl.gz', finished_at - started_at"
            " FROM raw_mercadolibre.runs WHERE snapshot_date = %s",
            (DAY1,),
        )
        row = cur.fetchone()
        assert row[:10] == (
            run1, "success", "MLA", ["MLA1055"], 7, False,
            {"highlights": 3, "products": 2, "offers": 6, "trends": 2},
            {"USER_PRODUCT": 1}, [], "success",
        )  # fmt: skip
        assert row[10] is True
        assert row[11].total_seconds() == 180
    assert audit_count(db_conn) == 10  # 2 days x 5 tables


BIG_PRICE = "123456789012345678.123456789012"  # 30 significant digits: a float keeps ~17


def test_payload_keeps_exact_numbers_and_wrong_types_become_null(db_conn, tmp_path):
    make_day(
        tmp_path,
        DAY1,
        n_offers=2,
        offer_overrides={"price": "12,5", "seller_id": True, "original_price": 0.1},
    )
    path = day_dir(tmp_path, DAY1) / "offers.jsonl.gz"
    lines = read_lines(path)
    obj = json.loads(lines[1])
    obj["price"] = "__BIG__"
    lines[1] = json.dumps(obj).replace('"__BIG__"', BIG_PRICE)
    write_lines(path, lines)
    assert float(BIG_PRICE) != Decimal(BIG_PRICE)  # the test value does not survive a float

    load(db_conn, prepare(tmp_path))
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT price, seller_id, original_price, payload->>'price',"
            " payload->'seller_id', payload->>'original_price'"
            " FROM raw_mercadolibre.offers ORDER BY line_no"
        )
        rows = cur.fetchall()
    assert rows[0] == (None, None, Decimal("0.1"), "12,5", True, "0.1")
    assert rows[1][0] == Decimal(BIG_PRICE)
    assert rows[1][3] == BIG_PRICE  # rendered by the server from the jsonb numeric


def test_copy_escaping_round_trips_in_typed_columns_and_payload(db_conn, tmp_path):
    tricky = "tab\there nl\nthere cr\r back\\slash \\N 'sq' \"dq\" ñandú 日本 😀"
    make_day(tmp_path, DAY1, n_offers=1)
    path = day_dir(tmp_path, DAY1) / "offers.jsonl.gz"
    lines = read_lines(path)
    obj = json.loads(lines[0])
    obj.update(item_id=tricky, seller_state="\\N", note=tricky)  # \N is COPY's NULL marker
    # Raw non-ASCII, a raw tab as JSON whitespace, and an escaped surrogate pair in the payload.
    line = "{\t" + json.dumps(obj, ensure_ascii=False)[1:-1] + ',"pair":"\\ud83d\\ude00"}'
    lines[0] = line
    write_lines(path, lines)

    load(db_conn, prepare(tmp_path))
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT item_id, seller_state, payload->>'item_id', payload->>'seller_state',"
            " payload->>'note', payload->>'pair', payload = %s::jsonb"
            " FROM raw_mercadolibre.offers",
            (line,),
        )
        assert cur.fetchone() == (tricky, "\\N", tricky, "\\N", tricky, "😀", True)


@pytest.mark.parametrize(
    "col_type, value, expected",
    [
        ("text", 5, None),
        ("integer", 2**31, None),
        ("integer", True, None),
        ("bigint", 2**40, 2**40),
        ("numeric", Decimal("1.10"), Decimal("1.10")),
        ("numeric", 3, Decimal(3)),
        ("numeric", Decimal("NaN"), None),
        ("numeric", False, None),
        ("boolean", 1, None),
        ("text[]", ["a", 1], None),
        ("text[]", [], []),
        ("timestamp with time zone", "2026-11-03T06:00:12", None),
        ("jsonb", {"a": 1}, {"a": 1}),
    ],
)
def test_convert_is_lenient(col_type, value, expected):
    assert ml_load.convert(col_type, value) == expected


def test_rerun_is_idempotent_and_skips_unchanged_days(
    db_conn, database_url, tmp_path, monkeypatch, caplog
):
    make_day(tmp_path, DAY1)
    make_day(tmp_path, DAY2, seed=10)
    with caplog.at_level(logging.INFO):
        assert run_main(monkeypatch, database_url, tmp_path) == 0
    assert database_url not in caplog.text
    first = full_content(db_conn)
    assert audit_count(db_conn) == 10

    summary = load(db_conn, prepare(tmp_path))
    assert summary.loaded == [] and len(summary.unchanged) == 2
    assert full_content(db_conn) == first
    assert audit_count(db_conn) == 10  # nothing rewritten, nothing audited

    assert run_main(monkeypatch, database_url, tmp_path, "--full-reload") == 0
    assert full_content(db_conn) == first  # same content, rewritten
    assert audit_count(db_conn) == 20


def test_reload_after_a_rerun_day_replaces_only_that_day(db_conn, tmp_path):
    make_day(tmp_path, DAY1)
    run2 = make_day(tmp_path, DAY2, seed=10)
    load(db_conn, prepare(tmp_path))
    before = counts(db_conn)

    new_run1 = make_day(tmp_path, DAY1, n_offers=5, seed=50)  # the snapshot job re-ran DAY1
    summary = load(db_conn, prepare(tmp_path))
    assert summary.loaded == [date(2026, 11, 3)]
    assert summary.unchanged == [date(2026, 11, 4)]
    after = counts(db_conn)
    assert after["offers"] == [(date(2026, 11, 3), 10, new_run1), (date(2026, 11, 4), 6, run2)]
    for table in ("runs", "highlights", "products", "trends"):
        assert after[table][0][2] == new_run1  # no rows of the old run survive
        assert after[table][1] == before[table][1]  # DAY2 untouched
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT min(item_id), max(item_id) FROM raw_mercadolibre.offers"
            " WHERE snapshot_date = %s",
            (DAY1,),
        )
        assert cur.fetchone() == ("MLA1000000050", "MLA1000000054")


def test_bad_folder_fails_the_run_with_nothing_changed(
    db_conn, database_url, tmp_path, monkeypatch
):
    make_day(tmp_path, DAY1)
    assert run_main(monkeypatch, database_url, tmp_path) == 0
    before = full_content(db_conn)

    make_day(tmp_path, DAY1, n_offers=4, seed=30)  # a changed, valid day ...
    make_day(tmp_path, DAY2)
    edit_run_json(  # ... and a bad one: the run fails as a whole
        tmp_path, DAY2, row_counts={"highlights": 3, "products": 2, "offers": 5, "trends": 2}
    )
    assert run_main(monkeypatch, database_url, tmp_path) == 1
    assert full_content(db_conn) == before
    assert audit_count(db_conn) == 5


def test_count_check_in_the_database_rolls_back(db_conn, tmp_path, monkeypatch):
    make_day(tmp_path, DAY1)
    days = prepare(tmp_path)
    load(db_conn, days)
    before = full_content(db_conn)
    make_day(tmp_path, DAY1, n_offers=4)
    days = prepare(tmp_path)
    # Simulate rows going missing between validation and COPY (e.g. a writer bug in iter_rows).
    real_iter = ml_load.iter_rows

    def drop_last(kind, *args):
        rows = list(real_iter(kind, *args))
        return rows[:-1] if kind == "offers" else rows

    monkeypatch.setattr(ml_load, "iter_rows", drop_last)
    with pytest.raises(LoadError, match="loaded 7 rows but run.json row_counts says 8"):
        load(db_conn, days)
    assert full_content(db_conn) == before


def test_file_changed_after_validation_rolls_back(db_conn, tmp_path):
    make_day(tmp_path, DAY1)
    days = prepare(tmp_path)
    path = day_dir(tmp_path, DAY1) / "trends.jsonl.gz"
    write_lines(path, read_lines(path)[:1])
    with pytest.raises(LoadError, match="file changed after validation"):
        load(db_conn, days)
    assert counts(db_conn) == {}  # even the schema creation rolled back


def test_failed_day_with_empty_files_is_loaded(db_conn, tmp_path):
    run_id = make_day(tmp_path, DAY1, status="failed")
    summary = load(db_conn, prepare(tmp_path))
    assert summary.loaded == [date(2026, 11, 3)]
    assert summary.rows == {"highlights": 0, "products": 0, "offers": 0, "trends": 0}
    assert counts(db_conn)["runs"] == [(date(2026, 11, 3), 1, run_id)]
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, errors->0->>'status' FROM raw_mercadolibre.runs")
        assert cur.fetchone() == ("failed", "500")


def test_loaded_day_without_folder_is_kept(db_conn, tmp_path, caplog):
    make_day(tmp_path, DAY1)
    make_day(tmp_path, DAY2)
    load(db_conn, prepare(tmp_path))
    before = full_content(db_conn)
    for path in sorted(day_dir(tmp_path, DAY1).iterdir()):
        path.unlink()
    day_dir(tmp_path, DAY1).rmdir()
    with caplog.at_level(logging.WARNING):
        summary = load(db_conn, prepare(tmp_path))
    assert summary.loaded == [] and summary.unchanged == [date(2026, 11, 4)]
    assert "1 loaded day(s) have no folder under --source and are kept: 2026-11-03" in caplog.text
    assert full_content(db_conn) == before


def test_drifted_data_table_is_recreated_and_every_day_reloaded(db_conn, tmp_path):
    make_day(tmp_path, DAY1)
    make_day(tmp_path, DAY2, seed=10)
    load(db_conn, prepare(tmp_path))
    before = full_content(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("ALTER TABLE raw_mercadolibre.offers ALTER COLUMN price TYPE double precision")
        cur.execute("DELETE FROM raw_mercadolibre.offers")  # what an old table might hold
    summary = load(db_conn, prepare(tmp_path))  # files unchanged, but offers was re-created
    assert summary.loaded == [date(2026, 11, 3), date(2026, 11, 4)]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT data_type FROM information_schema.columns WHERE table_schema ="
            " 'raw_mercadolibre' AND table_name = 'offers' AND column_name = 'price'"
        )
        assert cur.fetchone() == ("numeric",)
    assert full_content(db_conn) == before


def test_drifted_audit_table_is_never_dropped_and_checked_first(
    db_conn, database_url, tmp_path, monkeypatch
):
    make_day(tmp_path, DAY1)
    load(db_conn, prepare(tmp_path))
    with db_conn.cursor() as cur:
        cur.execute("ALTER TABLE raw_mercadolibre._load_audit ADD COLUMN note text")
        cur.execute("ALTER TABLE raw_mercadolibre.trends RENAME COLUMN keyword TO kw")
    with db_conn.cursor() as cur:
        cur.execute("SELECT * FROM raw_mercadolibre.trends ORDER BY line_no")
        trends_before = cur.fetchall()
    before = counts(db_conn)

    make_day(tmp_path, DAY1, n_offers=5)
    with pytest.raises(SchemaDriftError, match="append-only"):
        load(db_conn, prepare(tmp_path))
    assert run_main(monkeypatch, database_url, tmp_path) == 1
    assert audit_count(db_conn) == 5  # history kept
    assert counts(db_conn) == before  # nothing reloaded
    with db_conn.cursor() as cur:  # the drifted trends table was not touched either
        cur.execute("SELECT * FROM raw_mercadolibre.trends ORDER BY line_no")
        assert cur.fetchall() == trends_before


def test_dependent_view_blocks_a_recreate(db_conn, database_url, tmp_path, monkeypatch, caplog):
    make_day(tmp_path, DAY1)
    load(db_conn, prepare(tmp_path))
    with db_conn.cursor() as cur:
        cur.execute("CREATE SCHEMA staging_test")
        cur.execute("CREATE VIEW staging_test.v AS SELECT item_id FROM raw_mercadolibre.offers")
        cur.execute(
            "ALTER TABLE raw_mercadolibre.offers ALTER COLUMN tags TYPE text USING tags::text"
        )
    try:
        with caplog.at_level(logging.ERROR):
            assert run_main(monkeypatch, database_url, tmp_path) == 1
        assert "DependentObjectsStillExist" in caplog.text
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DROP SCHEMA staging_test CASCADE")


# --------------------------------------------------------------------------- orphans + drift


def remove_day(root: Path, day: str) -> None:
    for path in sorted(day_dir(root, day).iterdir()):
        path.unlink()
    day_dir(root, day).rmdir()


def table_rows(conn, table: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM raw_mercadolibre.{table} ORDER BY 1, 2")  # noqa: S608
        return cur.fetchall()


def all_rows(conn) -> dict[str, list[tuple]]:
    return {t: table_rows(conn, t) for t in ("runs", "highlights", "products", "offers", "trends")}


@pytest.mark.parametrize(
    "drift_sql",
    [
        # Review reproduction 1: runs is re-created, the file tables keep the orphan day silently.
        "ALTER TABLE raw_mercadolibre.runs ADD COLUMN note text",
        # Review reproduction 2: offers is re-created, the orphan day's offers are lost.
        "ALTER TABLE raw_mercadolibre.offers ALTER COLUMN price TYPE double precision",
        # A data table dropped by hand would be created empty: same loss.
        "DROP TABLE raw_mercadolibre.trends",
    ],
)
def test_drift_with_an_orphan_day_fails_with_nothing_changed(
    db_conn, database_url, tmp_path, monkeypatch, caplog, drift_sql
):
    make_day(tmp_path, DAY1)
    make_day(tmp_path, DAY2, seed=10)
    load(db_conn, prepare(tmp_path))
    remove_day(tmp_path, DAY1)  # e.g. a partial checkout of the data repo
    with db_conn.cursor() as cur:
        cur.execute(drift_sql)
    before = {t: table_rows(db_conn, t) for t in ("runs", "highlights", "products", "offers")}
    audit_before = audit_count(db_conn)

    with pytest.raises(LoadError, match="full data repo checkout") as excinfo:
        load(db_conn, prepare(tmp_path), full_reload=True)
    assert "1 stored day(s) have no folder under --source (2026-11-03)" in str(excinfo.value)
    with caplog.at_level(logging.ERROR):
        assert run_main(monkeypatch, database_url, tmp_path) == 1
    assert "load rolled back, nothing changed" in caplog.text
    assert "full data repo checkout" in caplog.text
    after = {t: table_rows(db_conn, t) for t in before}
    assert after == before  # the drifted table was not dropped, the orphan day kept everywhere
    assert {r[0] for r in after["offers"]} == {date(2026, 11, 3), date(2026, 11, 4)}
    assert audit_count(db_conn) == audit_before


def test_orphans_are_read_from_every_data_table(db_conn, tmp_path, caplog):
    make_day(tmp_path, DAY1)
    make_day(tmp_path, DAY2, seed=10)
    load(db_conn, prepare(tmp_path))
    with db_conn.cursor() as cur:  # the orphan day has rows in the file tables but no runs row
        cur.execute("DELETE FROM raw_mercadolibre.runs WHERE snapshot_date = %s", (DAY1,))
    remove_day(tmp_path, DAY1)
    before = all_rows(db_conn)
    with caplog.at_level(logging.WARNING):
        summary = load(db_conn, prepare(tmp_path))
    assert summary.unchanged == [date(2026, 11, 4)]
    assert "1 loaded day(s) have no folder under --source and are kept: 2026-11-03" in caplog.text
    assert all_rows(db_conn) == before

    with db_conn.cursor() as cur:  # ... and with a drift on top, the run now fails
        cur.execute("ALTER TABLE raw_mercadolibre.runs ADD COLUMN note text")
    with pytest.raises(LoadError, match=r"table\(s\) runs must be \(re-\)created"):
        load(db_conn, prepare(tmp_path))


def test_drifted_table_without_a_snapshot_date_column_is_not_dropped_blindly(db_conn, tmp_path):
    make_day(tmp_path, DAY1)
    load(db_conn, prepare(tmp_path))
    with db_conn.cursor() as cur:
        cur.execute("ALTER TABLE raw_mercadolibre.trends RENAME COLUMN snapshot_date TO day")
    before = table_rows(db_conn, "trends")
    with pytest.raises(LoadError, match="trends has rows but no snapshot_date date column"):
        load(db_conn, prepare(tmp_path))
    assert table_rows(db_conn, "trends") == before


# --------------------------------------------------------------------------- error logging


def test_real_copy_data_error_through_main_logs_no_row_content(
    db_conn, database_url, tmp_path, monkeypatch, caplog
):
    import psycopg

    make_day(tmp_path, DAY1)
    path = day_dir(tmp_path, DAY1) / "offers.jsonl.gz"
    lines = read_lines(path)
    obj = json.loads(lines[1])
    obj["note"] = f"{SECRET_LOOKING}\udc80"  # payload-only key; dumped as a \udc80 escape
    lines[1] = json.dumps(obj)
    write_lines(path, lines)
    # Switch the validation guard off so the server sees what it would otherwise be protected from.
    monkeypatch.setattr(ml_load, "unstorable_char", lambda value: None)

    with pytest.raises(psycopg.errors.InvalidTextRepresentation) as excinfo:
        load(db_conn, prepare(tmp_path))
    assert excinfo.value.sqlstate == "22P02"
    assert SECRET_LOOKING in str(excinfo.value)  # the raw error does quote the row

    with caplog.at_level(logging.DEBUG):
        assert run_main(monkeypatch, database_url, tmp_path) == 1
    assert "load rolled back, nothing changed: InvalidTextRepresentation (SQLSTATE 22P02)" in (
        caplog.text
    )
    assert SECRET_LOOKING not in caplog.text
    assert counts(db_conn) == {}  # even the schema creation rolled back


def test_main_unexpected_load_error_logs_only_its_type(
    db_conn, database_url, tmp_path, monkeypatch, caplog
):
    make_day(tmp_path, DAY1)

    def boom(conn, days, *, full_reload):
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("CREATE SCHEMA raw_mercadolibre")
            raise UnicodeEncodeError("utf-8", SECRET_LOOKING, 0, 1, "surrogates not allowed")

    monkeypatch.setattr(ml_load, "load", boom)
    with caplog.at_level(logging.DEBUG):
        assert run_main(monkeypatch, database_url, tmp_path) == 1
    assert "load rolled back, nothing changed: unexpected UnicodeEncodeError" in caplog.text
    assert SECRET_LOOKING not in caplog.text
    assert "Traceback" not in caplog.text
    with db_conn.cursor() as cur:
        cur.execute("SELECT to_regnamespace('raw_mercadolibre')")
        assert cur.fetchone() == (None,)
