"""Tests for scripts/summarize_run.py (run.json fixtures only, no payload data)."""

import json

import pytest

from scripts import summarize_run as sr


def make_run(**overrides):
    run = {
        "run_id": "00000000-0000-0000-0000-000000000000",
        "snapshot_date": "2026-11-04",
        "status": "success",
        "categories": ["MLA1055", "MLA1652"],
        "categories_requested": ["MLA1055", "MLA1652", "MLA1002"],
        "requests_made": 120,
        "request_cap": 1000,
        "request_cap_reached": False,
        "row_counts": {"highlights": 40, "products": 30, "offers": 500, "trends": 50},
        "skipped_highlights": {"ITEM": 10},
        "products_without_offers": 2,
        "errors": [],
    }
    run.update(overrides)
    return run


def write_run(tmp_path, run):
    path = tmp_path / "run.json"
    path.write_text(json.dumps(run), encoding="utf-8")
    return path


# ------------------------------------------------------------------ coverage warnings


def test_healthy_run_has_no_warnings():
    assert sr.coverage_warnings(make_run()) == []


def test_warns_when_every_followed_product_has_no_offers():
    run = make_run(products_without_offers=30, row_counts={"products": 30, "offers": 0})
    warnings = sr.coverage_warnings(run)
    assert len(warnings) == 1
    assert "30 of 30" in warnings[0]


def test_warns_when_offers_are_zero_even_if_counter_disagrees():
    run = make_run(products_without_offers=0, row_counts={"products": 5, "offers": 0})
    assert any("No offers captured" in w for w in sr.coverage_warnings(run))


def test_no_offer_warning_when_there_are_no_products():
    run = make_run(products_without_offers=0, row_counts={"products": 0, "offers": 0})
    assert sr.coverage_warnings(run) == []


def test_warns_on_partial_and_request_cap():
    run = make_run(
        status="partial",
        request_cap_reached=True,
        requests_made=1000,
        errors=[{"path": "/x", "status": 500, "count": 3}],
    )
    warnings = sr.coverage_warnings(run)
    assert any("partial" in w for w in warnings)
    assert any("1000/1000" in w for w in warnings)


def test_malformed_fields_do_not_crash():
    run = make_run(row_counts="nope", products_without_offers=None, errors="nope")
    assert sr.coverage_warnings(run) == []
    sr.summary_markdown(run, "0", [])


# ------------------------------------------------------------------ summary markdown


def test_summary_table_contains_counts_only():
    run = make_run(errors=[{"path": "/products/MLA123/items", "status": 500, "count": 2}])
    text = sr.summary_markdown(run, "0", [])
    assert "| status | success |" in text
    assert "| categories captured | 2 / 3 |" in text
    assert "| requests_made | 120 / 1000 |" in text
    assert "| rows: offers | 500 |" in text
    assert "| errors (entries / total) | 1 / 2 |" in text
    assert "MLA123" not in text  # error paths are not echoed
    assert run["run_id"] not in text


def test_summary_without_run_json():
    text = sr.summary_markdown(None, "2", [])
    assert "No `run.json`" in text
    assert "authorize" in text


# ------------------------------------------------------------------ CLI


def test_cli_summary_appends_to_step_summary_and_prints_warning(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    summary.write_text("previous\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    path = write_run(tmp_path, make_run(products_without_offers=4, row_counts={"products": 4}))
    assert sr.main(["summary", "--run-json", str(path), "--exit-code", "0"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("::warning title=ML snapshot::No offers captured")
    content = summary.read_text(encoding="utf-8")
    assert content.startswith("previous\n")
    assert "> **Warning:** No offers captured" in content


def test_cli_summary_missing_or_invalid_run_json_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert sr.main(["summary", "--run-json", str(tmp_path / "missing.json")]) == 0
    assert "No `run.json`" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert sr.main(["summary", "--run-json", str(bad), "--exit-code", "1"]) == 0


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("1", "check the run log"),
        ("2", "python -m ingestion.mercadolibre.authorize"),
        ("3", "already consumed"),
        ("4", "re-run the workflow later"),
        ("5", "ML_CLIENT_ID"),
        ("", "did not finish"),
        ("9", "Unexpected snapshot exit code 9"),
    ],
)
def test_check_fails_with_human_message(code, expected, capsys):
    assert sr.main(["check", "--exit-code", code]) == 1
    out = capsys.readouterr().out
    assert out.startswith("::error title=ML snapshot::")
    assert expected in out


def test_check_passes_on_zero(capsys):
    assert sr.main(["check", "--exit-code", "0"]) == 0
    assert "::error" not in capsys.readouterr().out


def test_annotation_escaping():
    assert sr._escape_annotation("a%b\nc\r") == "a%25b%0Ac%0D"
