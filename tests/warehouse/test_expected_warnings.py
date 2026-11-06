"""``python -m warehouse.ci.expected_warnings``: synthetic run_results.json, no dbt or DB."""

from __future__ import annotations

import json

import pytest

from warehouse.ci.expected_warnings import EXIT_FAILED, EXIT_OK, MAX_LISTED, main
from warehouse.ci.ml_fixture_days import EXPECTED_WARNINGS

PKG = "ecommerce_analytics"
COLUMNS = ("item_id", "seller_id", "price", "currency_id")


def _warning(column: str, failures: int = 1, status: str = "warn") -> dict:
    return {
        "unique_id": f"test.{PKG}.not_null_stg_mercadolibre__offers_{column}.0123456789",
        "status": status,
        "failures": failures,
        "message": "Got 1 result, configured to warn if != 0",
    }


def _base() -> list[dict]:
    """A clean build: a hook, models, passing tests, a unit test, and the four warnings."""
    return [
        {"unique_id": f"operation.{PKG}.{PKG}-on-run-start-0", "status": "success"},
        {"unique_id": f"model.{PKG}.stg_mercadolibre__offers", "status": "success"},
        {"unique_id": f"model.{PKG}.fact_offer_snapshot", "status": "success"},
        {
            "unique_id": f"test.{PKG}.not_null_fact_offer_snapshot_price.abc",
            "status": "pass",
            "failures": 0,
        },
        {"unique_id": f"unit_test.{PKG}.fact_offer_snapshot.rules", "status": "pass"},
        *(_warning(c) for c in COLUMNS),
    ]


def _run(tmp_path, results, capsys) -> tuple[int, str]:
    path = tmp_path / "run_results.json"
    path.write_text(json.dumps({"elapsed_time": 1.0, "results": results}))
    code = main(["--run-results", str(path)])
    out = capsys.readouterr()
    return code, out.out + out.err


def test_expected_set_is_the_four_malformed_offer_columns():
    assert EXPECTED_WARNINGS == {f"not_null_stg_mercadolibre__offers_{c}": 1 for c in COLUMNS}


def test_exact_set_passes(tmp_path, capsys):
    code, out = _run(tmp_path, _base(), capsys)
    assert code == EXIT_OK
    assert "ok" in out


@pytest.mark.parametrize("column", COLUMNS)
def test_missing_warning_fails(tmp_path, capsys, column):
    results = [r for r in _base() if not r["unique_id"].endswith(f"offers_{column}.0123456789")]
    code, out = _run(tmp_path, results, capsys)
    assert code == EXIT_FAILED
    assert f"missing expected warning: not_null_stg_mercadolibre__offers_{column}" in out
    assert "not in run_results" in out


def test_expected_test_passing_is_missing(tmp_path, capsys):
    results = [r for r in _base() if "offers_price." not in r["unique_id"]]
    results.append(_warning("price", failures=0, status="pass"))
    code, out = _run(tmp_path, results, capsys)
    assert code == EXIT_FAILED
    assert "missing expected warning: not_null_stg_mercadolibre__offers_price (status pass)" in out


@pytest.mark.parametrize(
    "unique_id",
    [
        f"test.{PKG}.not_null_stg_mercadolibre__offers_product_id.fff",
        f"test.{PKG}.accepted_values_stg_mercadolibre__highlights_type.eee",
        f"model.{PKG}.not_null_stg_mercadolibre__offers_price",  # right name, not a test
    ],
)
def test_extra_warning_fails(tmp_path, capsys, unique_id):
    results = [*_base(), {"unique_id": unique_id, "status": "warn", "failures": 2}]
    code, out = _run(tmp_path, results, capsys)
    assert code == EXIT_FAILED
    assert f"unexpected warning: {unique_id}" in out


def test_extra_warning_replacing_an_expected_one_fails(tmp_path, capsys):
    """Same WARN=4 count, different set: the case a count check cannot see."""
    results = [r for r in _base() if "offers_seller_id." not in r["unique_id"]]
    other = f"test.{PKG}.not_null_stg_mercadolibre__offers_product_id.fff"
    results.append({"unique_id": other, "status": "warn", "failures": 1})
    assert sum(r["status"] == "warn" for r in results) == 4
    code, out = _run(tmp_path, results, capsys)
    assert code == EXIT_FAILED
    assert f"unexpected warning: {other}" in out
    assert "missing expected warning: not_null_stg_mercadolibre__offers_seller_id" in out


@pytest.mark.parametrize("failures", [0, 2, 17, None])
def test_wrong_failure_count_fails(tmp_path, capsys, failures):
    results = [r for r in _base() if "offers_currency_id." not in r["unique_id"]]
    bad = _warning("currency_id")
    bad["failures"] = failures
    results.append(bad)
    code, out = _run(tmp_path, results, capsys)
    assert code == EXIT_FAILED
    assert "wrong failure count" in out
    assert f"failures={failures}, expected 1" in out


def test_duplicate_expected_warning_fails(tmp_path, capsys):
    code, out = _run(tmp_path, [*_base(), _warning("item_id")], capsys)
    assert code == EXIT_FAILED
    assert "reported 2 times" in out


@pytest.mark.parametrize("status", ["fail", "error", "runtime error", "skipped"])
@pytest.mark.parametrize(
    "unique_id",
    [
        f"test.{PKG}.assert_fact_offer_snapshot_reconciles_with_raw",
        f"model.{PKG}.fact_offer_snapshot",
        f"unit_test.{PKG}.fact_offer_snapshot.rules",
    ],
)
def test_failing_node_fails(tmp_path, capsys, status, unique_id):
    results = [r for r in _base() if r["unique_id"] != unique_id]
    results.append(
        {
            "unique_id": unique_id,
            "status": status,
            "failures": 3,
            "message": "secret-looking row value 'MLA123'",
        }
    )
    code, out = _run(tmp_path, results, capsys)
    assert code == EXIT_FAILED
    assert f"{status}: {unique_id}" in out
    assert "MLA123" not in out  # dbt's message is never printed


def test_expected_warning_with_fail_status_fails(tmp_path, capsys):
    """A severity change to error turns the warning into a fail: both are reported."""
    results = [r for r in _base() if "offers_item_id." not in r["unique_id"]]
    results.append(_warning("item_id", status="fail"))
    code, out = _run(tmp_path, results, capsys)
    assert code == EXIT_FAILED
    assert "fail: test.ecommerce_analytics.not_null_stg_mercadolibre__offers_item_id" in out
    assert (
        "missing expected warning: not_null_stg_mercadolibre__offers_item_id (status fail)" in out
    )


@pytest.mark.parametrize("content", ["", "not json", json.dumps({"results": []}), "[]"])
def test_unreadable_or_empty_run_results_fails(tmp_path, capsys, content):
    path = tmp_path / "run_results.json"
    path.write_text(content)
    assert main(["--run-results", str(path)]) == EXIT_FAILED


def test_absent_run_results_fails(tmp_path):
    assert main(["--run-results", str(tmp_path / "absent.json")]) == EXIT_FAILED


def test_usage_error_exits_2():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_long_lists_are_capped_root_causes_first(tmp_path, capsys):
    """One failure can skip dozens of nodes: the failure is listed first, the rest capped."""
    skipped = [{"unique_id": f"test.{PKG}.t{i}", "status": "skipped"} for i in range(60)]
    root = {"unique_id": f"model.{PKG}.fact_offer_snapshot", "status": "error"}
    results = [r for r in _base() if r["unique_id"] != root["unique_id"]]
    code, out = _run(tmp_path, [*skipped, *results, root], capsys)
    assert code == EXIT_FAILED
    lines = [line for line in out.splitlines() if line.startswith("  - ")]
    assert len(lines) == MAX_LISTED
    assert lines[0] == f"  - error: {root['unique_id']}"
    assert f"... and {61 - MAX_LISTED} more" in out
