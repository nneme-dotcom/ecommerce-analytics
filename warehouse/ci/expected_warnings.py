"""Check that the fixture ``dbt build`` warned about exactly the expected tests (ADR-016, ADR-019).

    dbt build --project-dir warehouse --profiles-dir warehouse
    python -m warehouse.ci.expected_warnings --run-results warehouse/target/run_results.json

``dbt build`` exits 0 on warnings, so a WARN count alone can hide a regression: an unexpected
warning can replace an expected one at the same count. This reads dbt's ``run_results.json`` and
passes only if

- the tests with status ``warn`` are exactly ``EXPECTED_WARNINGS`` (defined in
  ``warehouse/ci/ml_fixture_days.py``, next to the malformed fixture offers that cause them),
- each of them has the expected ``failures`` count (1: one malformed offer each), and
- no node has status ``fail``, ``error``, ``runtime error`` or ``skipped`` (dbt skips a node only
  when something upstream failed).

Test names are matched on the third part of the dbt ``unique_id``
(``test.<package>.<name>[.<hash>]``). Messages name node ids, statuses and counts only: never
dbt's ``message`` or compiled SQL, never row data.

Exit codes: 0 ok · 1 unexpected/missing warnings, a failing node, or run_results unreadable ·
2 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from warehouse.ci.ml_fixture_days import EXPECTED_WARNINGS

EXIT_OK = 0
EXIT_FAILED = 1
FAILING_STATUSES = ("fail", "error", "runtime error", "skipped")
MAX_LISTED = 20  # as scripts.warehouse_report.MAX_LISTED_NODES; one failure can skip dozens


@dataclass(frozen=True)
class Result:
    unique_id: str
    status: str
    failures: int | None

    @property
    def resource_type(self) -> str:
        return self.unique_id.split(".", 1)[0]

    @property
    def name(self) -> str:
        parts = self.unique_id.split(".")
        return parts[2] if len(parts) >= 3 else self.unique_id


def read_results(run_results: Path) -> list[Result]:
    """The node results of a dbt ``run_results.json`` (same reading as
    ``scripts.warehouse_report.dbt_result_counts``, plus ``failures``)."""
    data = json.loads(run_results.read_text(encoding="utf-8"))
    raw: list[dict[str, Any]] = data.get("results", []) if isinstance(data, dict) else []
    results = []
    for item in raw:
        failures = item.get("failures")
        results.append(
            Result(
                unique_id=str(item.get("unique_id", "?")),
                status=str(item.get("status", "?")),
                failures=failures if isinstance(failures, int) else None,
            )
        )
    return results


def problems(results: list[Result], expected: Mapping[str, int] | None = None) -> list[str]:
    """Every deviation from the expected outcome (``EXPECTED_WARNINGS`` by default), one line
    each; empty when the build is as expected."""
    expected = EXPECTED_WARNINGS if expected is None else expected
    found: list[str] = []
    if not results:
        return ["run_results has no results (did dbt build run?)"]
    # Root causes first; skipped nodes (consequences of a failure upstream) last.
    for r in results:
        if r.status in FAILING_STATUSES and r.status != "skipped":
            found.append(f"{r.status}: {r.unique_id}")
    warned: dict[str, list[Result]] = {}
    for r in results:
        if r.status == "warn":
            if r.resource_type == "test" and r.name in expected:
                warned.setdefault(r.name, []).append(r)
            else:
                found.append(f"unexpected warning: {r.unique_id}")
    by_name = {r.name: r for r in results if r.resource_type == "test"}
    for name, want in sorted(expected.items()):
        hits = warned.get(name, [])
        if not hits:
            actual = by_name.get(name)
            state = f"status {actual.status}" if actual else "not in run_results"
            found.append(f"missing expected warning: {name} ({state})")
            continue
        if len(hits) > 1:
            found.append(f"expected warning reported {len(hits)} times: {name}")
        for r in hits:
            if r.failures != want:
                found.append(
                    f"wrong failure count: {r.unique_id} has failures={r.failures}, expected {want}"
                )
    found += [f"skipped: {r.unique_id}" for r in results if r.status == "skipped"]
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m warehouse.ci.expected_warnings",
        description="Fail unless the fixture dbt build warned about exactly the expected tests.",
    )
    parser.add_argument("--run-results", type=Path, required=True, help="dbt run_results.json")
    args = parser.parse_args(argv)
    try:
        results = read_results(args.run_results)
    except (OSError, ValueError) as exc:  # JSONDecodeError is a ValueError
        print(
            f"expected_warnings: cannot read {args.run_results} ({type(exc).__name__})",
            file=sys.stderr,
        )
        return EXIT_FAILED
    found = problems(results)
    if found:
        print(
            f"expected_warnings: the fixture dbt build differs from the expected outcome "
            f"({len(found)} problem(s); expected exactly these warnings, 1 failing row each: "
            f"{', '.join(sorted(EXPECTED_WARNINGS))}):",
            file=sys.stderr,
        )
        for line in found[:MAX_LISTED]:
            print(f"  - {line}", file=sys.stderr)
        if len(found) > MAX_LISTED:
            print(f"  ... and {len(found) - MAX_LISTED} more", file=sys.stderr)
        return EXIT_FAILED
    print(
        f"expected_warnings: ok, {len(results)} results; exactly the {len(EXPECTED_WARNINGS)} "
        "expected warnings (1 failing row each), no fail/error/skipped"
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
