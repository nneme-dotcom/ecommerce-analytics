"""Tests for scripts/warehouse_report.py: load plan (daily/full) and the job summary.

The DB tests use the throwaway database of tests/conftest.py, filled by the real loaders with the
warehouse CI fixtures (warehouse/fixtures, synthetic transactional rows).
"""

from __future__ import annotations

import json
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from ingestion.olist.files import OLIST_FILES
from scripts import warehouse_report as wr

FIXTURES = Path(__file__).resolve().parents[1] / "warehouse" / "fixtures" / "raw_olist"
# The fixtures hold AR holidays for 2026 only: "today" is pinned so the plan stays deterministic.
TODAY = date(2026, 11, 4)


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    monkeypatch.setattr(wr, "utc_today", lambda: TODAY)


def make_zip(path: Path, *, change: str | None = None, drop: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for spec in OLIST_FILES:
            if spec.filename == drop:
                continue
            data = (FIXTURES / spec.filename).read_bytes()
            if spec.filename == change:
                data += b"\n"  # different bytes, same header: a changed input

            zf.writestr(spec.filename, data)
    return path


def load_fixtures(conn) -> None:
    from warehouse.ci import fixture_db

    fixture_db.load_fixtures(conn, fixture_db.prepare_fixtures())


def read_outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line)


# --------------------------------------------------------------------------- pure parts


def test_archive_csv_hashes_match_the_loader_hashes(tmp_path):
    from ingestion.olist.download import sha256_file

    hashes = wr.archive_csv_hashes(make_zip(tmp_path / "a.zip"))
    assert hashes == {s.table: sha256_file(FIXTURES / s.filename) for s in OLIST_FILES}


@pytest.mark.parametrize("kind", ["missing_member", "not_a_zip", "absent"])
def test_archive_csv_hashes_unreadable_is_none(tmp_path, kind):
    path = tmp_path / "a.zip"
    if kind == "missing_member":
        make_zip(path, drop=OLIST_FILES[0].filename)
    elif kind == "not_a_zip":
        path.write_bytes(b"version https://git-lfs.github.com/spec/v1\n")
    assert wr.archive_csv_hashes(path) is None


def test_dbt_result_counts(tmp_path):
    results = {
        "elapsed_time": 12.4,
        "results": [
            {"unique_id": "model.p.stg_a", "status": "success", "message": "SELECT 3"},
            {"unique_id": "test.p.t1", "status": "pass"},
            {"unique_id": "test.p.t2", "status": "fail", "message": "Got 2 results"},
            {"unique_id": "test.p.t3", "status": "warn"},
            {"unique_id": "model.p.stg_b", "status": "error", "message": "boom 'row value'"},
            {"unique_id": "model.p.stg_c", "status": "skipped"},
            {"unique_id": "operation.p.hook", "status": "success"},
        ],
    }
    path = tmp_path / "run_results.json"
    path.write_text(json.dumps(results))
    counts, failing, elapsed = wr.dbt_result_counts(path)
    assert counts == Counter(
        {
            ("model", "success"): 1,
            ("model", "error"): 1,
            ("model", "skipped"): 1,
            ("test", "pass"): 1,
            ("test", "fail"): 1,
            ("test", "warn"): 1,
            ("operation", "success"): 1,
        }
    )
    assert failing == ["test.p.t2 (fail)", "test.p.t3 (warn)", "model.p.stg_b (error)"]
    assert elapsed == pytest.approx(12.4)
    assert wr.dbt_result_counts(tmp_path / "absent.json") is None


def test_render_summary_has_counts_and_no_messages():
    dbt = (Counter({("model", "success"): 2, ("test", "fail"): 1}), ["test.p.t (fail)"], 3.0)
    text, notes = wr.render_summary(
        mode="daily",
        steps={"raw_mercadolibre": "success", "raw_olist": ""},
        rows=[("raw_olist", "orders", 99441), ("raw_mercadolibre", "offers", 2313)],
        days=(2, "2026-11-04"),
        size=(310_000_000, [("raw_olist", 250_000_000), ("staging", 0)]),
        dbt=dbt,
        manifest={"archive_sha256": "ab" * 32, "files": {}},
        db_error=None,
    )
    assert "| `raw_olist.orders` | 99,441 |" in text
    assert "| raw_olist | not run |" in text
    assert "ML snapshot days loaded: **2** (latest 2026-11-04)" in text
    assert "| model | success | 2 |" in text and "| test | fail | 1 |" in text
    assert "`test.p.t (fail)`" in text
    assert "**310.0 MB** (31% of the 1 GB free plan)" in text
    assert "ab" * 32 in text
    assert notes == []


def test_render_summary_storage_warning_and_db_error():
    text, notes = wr.render_summary(
        mode="full",
        steps={},
        rows=None,
        days=None,
        size=(850_000_000, []),
        dbt=None,
        manifest=None,
        db_error="OperationalError",
    )
    assert "dbt did not run" in text
    assert any(n.startswith("::warning title=Warehouse storage::") for n in notes)
    assert any(n.startswith("::error title=Warehouse summary::") for n in notes)


def test_render_summary_caps_listed_nodes():
    failing = [f"test.p.t{i} (fail)" for i in range(30)]
    text, _ = wr.render_summary(
        mode="daily",
        steps={},
        rows=[],
        days=None,
        size=None,
        dbt=(Counter({("test", "fail"): 30}), failing, None),
        manifest=None,
        db_error=None,
    )
    assert "`test.p.t19 (fail)`" in text and "t20 (fail)" not in text
    assert "(and 10 more)" in text


def test_report_error_annotations_are_escaped():
    _, notes = wr.render_summary(
        mode="daily",
        steps={},
        rows=None,
        days=None,
        size=None,
        dbt=None,
        manifest=None,
        db_error="Odd%0AError\n::warning::x",
    )
    (error,) = [n for n in notes if n.startswith("::error")]
    assert "\n" not in error and "Odd%250AError%0A::warning::x" in error


# --------------------------------------------------------------------------- DB: plan


def test_full_mode_loads_everything_without_touching_the_db(tmp_path):
    plan = wr.decide("full", tmp_path / "absent.zip", cur=None)
    assert (plan.olist, plan.enrichment) == (True, True)


def test_daily_on_an_empty_warehouse_loads_olist_and_enrichment(db_conn, tmp_path):
    with db_conn.cursor() as cur:
        plan = wr.decide("daily", make_zip(tmp_path / "a.zip"), cur)
    assert (plan.olist, plan.enrichment) == (True, True)


def test_daily_with_unchanged_inputs_loads_neither(db_conn, tmp_path):
    load_fixtures(db_conn)
    with db_conn.cursor() as cur:
        plan = wr.decide("daily", make_zip(tmp_path / "a.zip"), cur)
    assert (plan.olist, plan.enrichment) == (False, False)
    assert any("unchanged" in r for r in plan.reasons)


def test_daily_with_a_changed_csv_loads_olist(db_conn, tmp_path):
    load_fixtures(db_conn)
    zip_path = make_zip(tmp_path / "a.zip", change="olist_orders_dataset.csv")
    with db_conn.cursor() as cur:
        plan = wr.decide("daily", zip_path, cur)
    assert (plan.olist, plan.enrichment) == (True, False)


def test_daily_with_an_unreadable_archive_runs_the_loader(db_conn, tmp_path):
    load_fixtures(db_conn)
    zip_path = tmp_path / "a.zip"
    zip_path.write_bytes(b"not a zip")
    with db_conn.cursor() as cur:
        plan = wr.decide("daily", zip_path, cur)
    assert plan.olist is True


def test_daily_without_the_archive_warns_and_skips_olist(db_conn, tmp_path):
    load_fixtures(db_conn)
    with db_conn.cursor() as cur:
        plan = wr.decide("daily", tmp_path / "absent.zip", cur)
    assert plan.olist is False
    assert any(r.startswith("::warning title=Olist archive missing::") for r in plan.reasons)


def test_daily_with_a_dropped_raw_table_reloads(db_conn, tmp_path):
    load_fixtures(db_conn)
    db_conn.execute("DROP TABLE raw_olist.sellers")
    db_conn.execute("DROP TABLE raw_enrichment.public_holidays")
    with db_conn.cursor() as cur:
        plan = wr.decide("daily", make_zip(tmp_path / "a.zip"), cur)
    assert (plan.olist, plan.enrichment) == (True, True)


def test_daily_with_only_fx_audited_loads_enrichment(db_conn, tmp_path):
    load_fixtures(db_conn)
    db_conn.execute("DELETE FROM raw_enrichment._load_audit WHERE table_name = 'public_holidays'")
    with db_conn.cursor() as cur:
        plan = wr.decide("daily", make_zip(tmp_path / "a.zip"), cur)
    assert plan.enrichment is True


def test_daily_in_a_new_year_without_its_ar_holidays_loads_enrichment(db_conn, tmp_path):
    load_fixtures(db_conn)
    zip_path = make_zip(tmp_path / "a.zip")
    with db_conn.cursor() as cur:
        same_year = wr.decide("daily", zip_path, cur, today=date(2026, 12, 31))
        new_year = wr.decide("daily", zip_path, cur, today=date(2027, 1, 1))
    assert (same_year.olist, same_year.enrichment) == (False, False)
    assert (new_year.olist, new_year.enrichment) == (False, True)
    assert any("no AR holidays for 2027" in r for r in new_year.reasons)


def test_daily_with_only_br_holidays_for_the_year_still_loads_enrichment(db_conn, tmp_path):
    # BR rows for the year do not count: the check is per country (AR = Mercado Libre).
    load_fixtures(db_conn)
    db_conn.execute("DELETE FROM raw_enrichment.public_holidays WHERE country_code = 'AR'")
    db_conn.execute(
        "INSERT INTO raw_enrichment.public_holidays (country_code, holiday_date, name, is_global,"
        " holiday_types, source) VALUES ('BR', '2026-01-01', 'x', true, '{Public}', 'test')"
    )
    with db_conn.cursor() as cur:
        plan = wr.decide("daily", make_zip(tmp_path / "a.zip"), cur, today=TODAY)
    assert plan.enrichment is True


def test_plan_reads_the_db_only(db_conn, tmp_path):
    load_fixtures(db_conn)
    db_conn.execute("SET default_transaction_read_only = on")
    try:
        with db_conn.cursor() as cur:
            plan = wr.decide("daily", make_zip(tmp_path / "a.zip"), cur, today=date(2027, 1, 1))
    finally:
        db_conn.execute("SET default_transaction_read_only = off")
    assert plan.enrichment is True


def test_plan_cli_writes_only_booleans_to_github_output(
    db_conn, database_url, tmp_path, monkeypatch
):
    load_fixtures(db_conn)
    monkeypatch.setenv("DATABASE_URL", database_url)
    out = tmp_path / "out"
    out.write_text("")
    code = wr.main(
        ["plan", "--mode", "daily", "--olist-zip", str(make_zip(tmp_path / "a.zip"))],
        {"GITHUB_OUTPUT": str(out)},
    )
    assert code == 0
    assert read_outputs(out) == {"olist": "false", "enrichment": "false"}


def test_plan_cli_new_year_requests_enrichment(db_conn, database_url, tmp_path, monkeypatch):
    load_fixtures(db_conn)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr(wr, "utc_today", lambda: date(2027, 1, 2))
    out = tmp_path / "out"
    out.write_text("")
    code = wr.main(
        ["plan", "--mode", "daily", "--olist-zip", str(make_zip(tmp_path / "a.zip"))],
        {"GITHUB_OUTPUT": str(out)},
    )
    assert code == 0
    assert read_outputs(out) == {"olist": "false", "enrichment": "true"}


def test_plan_cli_db_error_is_type_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://s3cr3tuser:pw@127.0.0.1:1/s3cr3tdb")
    out = tmp_path / "out"
    out.write_text("")
    code = wr.main(
        ["plan", "--mode", "daily", "--olist-zip", str(tmp_path / "a.zip")],
        {"GITHUB_OUTPUT": str(out)},
    )
    assert code == 1
    printed = capsys.readouterr().out
    assert "::error title=Warehouse plan::" in printed
    assert "s3cr3t" not in printed and "127.0.0.1" not in printed
    assert out.read_text() == ""


def test_plan_cli_without_database_url_is_a_config_error(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert wr.main(["plan", "--mode", "full", "--olist-zip", "x"], {}) == 2


# --------------------------------------------------------------------------- DB: summary


def test_summary_cli_counts_every_raw_table(db_conn, database_url, tmp_path, monkeypatch):
    load_fixtures(db_conn)
    monkeypatch.setenv("DATABASE_URL", database_url)
    summary = tmp_path / "summary.md"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"archive_sha256": "cd" * 32}))
    code = wr.main(
        [
            "summary",
            "--mode",
            "full",
            "--run-results",
            str(tmp_path / "absent.json"),
            "--olist-manifest",
            str(manifest),
            "--step",
            "raw_olist=success",
        ],
        {"GITHUB_STEP_SUMMARY": str(summary)},
    )
    assert code == 0
    text = summary.read_text()
    orders = db_conn.execute("SELECT count(*) FROM raw_olist.orders").fetchone()[0]
    assert f"| `raw_olist.orders` | {orders:,} |" in text
    assert "`raw_enrichment.fx_rates`" in text and "`raw_olist._load_audit`" in text
    assert "`pg_database_size`" in text
    assert "cd" * 32 in text
    assert "dbt did not run" in text
    # No database name, host or URL in the summary.
    dbname = db_conn.info.dbname
    assert dbname not in text and "127.0.0.1" not in text and "postgresql://" not in text


def test_summary_cli_db_unreachable_still_writes_and_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://s3cr3tuser:pw@127.0.0.1:1/s3cr3tdb")
    summary = tmp_path / "summary.md"
    code = wr.main(["summary", "--mode", "daily"], {"GITHUB_STEP_SUMMARY": str(summary)})
    assert code == 1
    text = summary.read_text() + capsys.readouterr().out
    assert "Database not reachable" in text
    assert "s3cr3t" not in text and "127.0.0.1" not in text
