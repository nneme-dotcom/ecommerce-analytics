"""Check that the repo secrets needed by the daily snapshot are present and usable.

Run by the manual `check-secrets` workflow. Prints PASS/FAIL per check, never a secret value.
It never uses ML_REFRESH_TOKEN against the API: refresh tokens are single-use and rotate,
so spending one here would break the next scheduled run. That one is checked by format only.

Exit code: 0 if every check passes, 1 otherwise.
"""

from __future__ import annotations

import os
import re
import subprocess  # noqa: S404 - fixed git command, no shell
import sys
from collections.abc import Callable
from dataclasses import dataclass

import httpx

TOKEN_URL = "https://api.mercadolibre.com/oauth/token"  # noqa: S105 - URL, not a secret
GITHUB_API = "https://api.github.com"
MAIN_REPO = "nneme-dotcom/ecommerce-analytics"
DATA_REPO = "nneme-dotcom/ecommerce-analytics-data"
# A deliberately invalid refresh token: lets us test the client id/secret pair without
# consuming the real refresh token.
PROBE_REFRESH_TOKEN = "TG-check-secrets-invalid-on-purpose"  # noqa: S105


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


def check_format(env: dict[str, str]) -> list[Result]:
    rules = {
        "ML_CLIENT_ID": (r"^\d{6,20}$", "digits only"),
        "ML_CLIENT_SECRET": (r"^[A-Za-z0-9]{20,64}$", "20-64 letters/digits"),
        "ML_REFRESH_TOKEN": (r"^TG-[A-Za-z0-9-]{10,}$", "starts with TG-"),
        "DATA_REPO_PAT": (r"^(github_pat_|ghp_)[A-Za-z0-9_]{20,}$", "starts with github_pat_"),
    }
    results = []
    for name, (pattern, expected) in rules.items():
        value = env.get(name, "")
        if not value:
            results.append(Result(f"{name} present", False, "missing or empty"))
        elif value != value.strip():
            results.append(Result(f"{name} format", False, "has leading/trailing spaces"))
        elif not re.match(pattern, value):
            results.append(
                Result(f"{name} format", False, f"unexpected format (expected {expected})")
            )
        else:
            results.append(Result(f"{name} format", True, expected))
    return results


def check_ml_client(env: dict[str, str], http: httpx.Client) -> Result:
    """`invalid_grant` means Mercado Libre accepted the client id/secret and only rejected
    our fake refresh token. Any client-level error means the pair is wrong."""
    name = "ML_CLIENT_ID + ML_CLIENT_SECRET accepted by Mercado Libre"
    if not env.get("ML_CLIENT_ID") or not env.get("ML_CLIENT_SECRET"):
        return Result(name, False, "skipped: missing secret")
    try:
        resp = http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": env["ML_CLIENT_ID"],
                "client_secret": env["ML_CLIENT_SECRET"],
                "refresh_token": PROBE_REFRESH_TOKEN,
            },
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        return Result(name, False, f"network error: {type(exc).__name__}")
    try:
        error = (resp.json() or {}).get("error", "")
    except ValueError:
        error = ""
    if error == "invalid_grant":
        return Result(name, True, "client credentials valid (fake refresh token rejected)")
    return Result(name, False, f"HTTP {resp.status_code}, error={error or 'none'}")


def check_pat_secrets(env: dict[str, str], http: httpx.Client) -> Result:
    name = f"DATA_REPO_PAT can read Actions secrets of {MAIN_REPO}"
    pat = env.get("DATA_REPO_PAT")
    if not pat:
        return Result(name, False, "skipped: missing secret")
    try:
        resp = http.get(
            f"{GITHUB_API}/repos/{MAIN_REPO}/actions/secrets/public-key",
            headers={"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"},
        )
    except httpx.HTTPError as exc:
        return Result(name, False, f"network error: {type(exc).__name__}")
    if resp.status_code == 200:
        return Result(name, True, "Secrets permission granted (write tested on first run)")
    hint = "add 'Secrets: Read and write' for this repo"
    return Result(name, False, f"HTTP {resp.status_code}: {hint}")


def check_pat_push(env: dict[str, str], run: Callable[..., subprocess.CompletedProcess]) -> Result:
    """`git push --dry-run` asks GitHub for the push endpoint, which requires write access,
    without changing anything."""
    name = f"DATA_REPO_PAT can push to {DATA_REPO}"
    pat = env.get("DATA_REPO_PAT")
    if not pat:
        return Result(name, False, "skipped: missing secret")
    url = f"https://x-access-token:{pat}@github.com/{DATA_REPO}.git"
    proc = run(
        ["git", "push", "--dry-run", url, "HEAD:refs/heads/check-secrets-dry-run"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode == 0:
        return Result(name, True, "Contents write access confirmed (dry run, nothing pushed)")
    stderr = (proc.stderr or "").replace(pat, "***")
    hint = "403" if "403" in stderr else "not found" if "not found" in stderr.lower() else "error"
    fix = "add 'Contents: Read and write'"
    return Result(name, False, f"git push --dry-run failed ({hint}): {fix}")


def main() -> int:
    env = dict(os.environ)
    results = check_format(env)
    with httpx.Client(timeout=20) as http:
        results.append(check_ml_client(env, http))
        results.append(check_pat_secrets(env, http))
    results.append(check_pat_push(env, subprocess.run))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = ["| Check | Result | Detail |", "|---|---|---|"]
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        print(f"[{mark}] {r.name}: {r.detail}")
        lines.append(f"| {r.name} | {'✅' if r.ok else '❌'} | {r.detail} |")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("## Secrets check\n\n" + "\n".join(lines) + "\n")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
