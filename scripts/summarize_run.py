"""Turn a snapshot run into a GitHub Actions job summary and annotations.

Used by `.github/workflows/ml-daily-snapshot.yml`. Standard library only, so it runs before or
without the project dependencies.

    python scripts/summarize_run.py summary --run-json PATH --exit-code CODE
        Appends a Markdown table to $GITHUB_STEP_SUMMARY (stdout if unset) and prints
        `::warning::` annotations. Always exits 0: a summary must never break the run.

    python scripts/summarize_run.py check --exit-code CODE
        Prints a human message for the snapshot exit code (ADR-008) and exits 1 unless CODE is 0.

Only counts and statuses are reported: never payload data, never tokens.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

EXIT_MESSAGES: dict[str, str] = {
    "0": "Snapshot finished (success or partial; see run.json status).",
    "1": (
        "Snapshot failed (no category captured, or aborted after the token was saved). "
        "The rotated refresh token was saved; check the run log and run.json errors."
    ),
    "2": (
        "Re-authorization required: run `python -m ingestion.mercadolibre.authorize` "
        "and update the ML_REFRESH_TOKEN secret."
    ),
    "3": (
        "The new refresh token could not be saved and the old one is already consumed: "
        "run `python -m ingestion.mercadolibre.authorize` and update the ML_REFRESH_TOKEN secret."
    ),
    "4": (
        "Token endpoint unavailable after retries (transient). The old refresh token is probably "
        "still valid: re-run the workflow later. If that run exits 2, re-authorize."
    ),
    "5": (
        "Configuration error: ML_CLIENT_ID, ML_CLIENT_SECRET or ML_REFRESH_TOKEN is missing. "
        "Check the repository secrets."
    ),
}
NOT_FINISHED_MESSAGE = (
    "The snapshot step did not finish (killed, timed out or cancelled). If a rotated token was "
    "written it has been saved by the token step; check its log."
)


def exit_message(code: str) -> str:
    code = code.strip()
    if not code:
        return NOT_FINISHED_MESSAGE
    return EXIT_MESSAGES.get(code, f"Unexpected snapshot exit code {code}.")


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def coverage_warnings(run: dict[str, Any]) -> list[str]:
    """Warnings for runs that look healthy by status but carry no usable offer data."""
    row_counts = run.get("row_counts") if isinstance(run.get("row_counts"), dict) else {}
    products = _int(row_counts.get("products"))
    offers = _int(row_counts.get("offers"))
    without_offers = _int(run.get("products_without_offers"))
    warnings: list[str] = []
    if products > 0 and (offers == 0 or without_offers >= products):
        warnings.append(
            f"No offers captured: {without_offers} of {products} followed products returned no "
            f"offers (offers rows: {offers}). The offers endpoint may have changed or be blocked."
        )
    if run.get("status") == "partial":
        errors = run.get("errors") if isinstance(run.get("errors"), list) else []
        warnings.append(f"Snapshot is partial: {len(errors)} error entries in run.json.")
    if run.get("request_cap_reached") is True:
        made, cap = _int(run.get("requests_made")), _int(run.get("request_cap"))
        warnings.append(f"Request cap reached ({made}/{cap}); the snapshot is incomplete.")
    return warnings


def summary_markdown(run: dict[str, Any] | None, exit_code: str, warnings: list[str]) -> str:
    lines = ["## Mercado Libre daily snapshot", ""]
    lines.append(f"Exit code: `{exit_code.strip() or 'none'}` — {exit_message(exit_code)}")
    lines.append("")
    if run is None:
        lines.append("No `run.json` for this date (the job did not reach the write step).")
        return "\n".join(lines) + "\n"
    row_counts = run.get("row_counts") if isinstance(run.get("row_counts"), dict) else {}
    errors = run.get("errors") if isinstance(run.get("errors"), list) else []
    error_total = sum(_int(e.get("count")) or 1 for e in errors if isinstance(e, dict))
    categories = run.get("categories") if isinstance(run.get("categories"), list) else []
    requested = run.get("categories_requested")
    requested = requested if isinstance(requested, list) else []
    rows = [
        ("snapshot_date", str(run.get("snapshot_date", ""))),
        ("status", str(run.get("status", ""))),
        ("categories captured", f"{len(categories)} / {len(requested)}"),
        ("requests_made", f"{_int(run.get('requests_made'))} / {_int(run.get('request_cap'))}"),
    ]
    for kind in ("highlights", "products", "offers", "trends"):
        rows.append((f"rows: {kind}", str(_int(row_counts.get(kind)))))
    rows.append(("products_without_offers", str(_int(run.get("products_without_offers")))))
    rows.append(("errors (entries / total)", f"{len(errors)} / {error_total}"))
    lines += ["| field | value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    if warnings:
        lines.append("")
        lines += [f"> **Warning:** {w}" for w in warnings]
    return "\n".join(lines) + "\n"


def _escape_annotation(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def load_run(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cmd_summary(run_json: Path, exit_code: str) -> int:
    run = load_run(run_json)
    warnings = coverage_warnings(run) if run is not None else []
    for warning in warnings:
        print(f"::warning title=ML snapshot::{_escape_annotation(warning)}")
    text = summary_markdown(run, exit_code, warnings)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


def cmd_check(exit_code: str) -> int:
    message = exit_message(exit_code)
    if exit_code.strip() == "0":
        print(message)
        return 0
    print(f"::error title=ML snapshot::{_escape_annotation(message)}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/summarize_run.py")
    sub = parser.add_subparsers(dest="command", required=True)
    p_summary = sub.add_parser("summary")
    p_summary.add_argument("--run-json", type=Path, required=True)
    p_summary.add_argument("--exit-code", default="")
    p_check = sub.add_parser("check")
    p_check.add_argument("--exit-code", default="")
    args = parser.parse_args(argv)
    if args.command == "summary":
        return cmd_summary(args.run_json, args.exit_code)
    return cmd_check(args.exit_code)


if __name__ == "__main__":
    sys.exit(main())
