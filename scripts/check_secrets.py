"""Check that the repo secrets needed by the daily snapshot are present and usable.

Run by the manual `check-secrets` workflow. Prints PASS/FAIL per check, never a secret value.
It never uses ML_REFRESH_TOKEN against the API: refresh tokens are single-use and rotate,
so spending one here would break the next scheduled run. That one is checked by format only.

Exit code: 0 if every check passes, 1 otherwise.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
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
        "DATA_REPO_PAT": (
            r"^(github_pat_|ghp_)[A-Za-z0-9_]{20,}$", "starts with github_pat_ or ghp_"
        ),
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


PROBE_SECRET_NAME = "CHECK_SECRETS_PROBE"  # noqa: S105 - name of a throwaway secret
Runner = Callable[..., subprocess.CompletedProcess]


def _run_safely(run: Runner, cmd: list[str], *, env: dict[str, str], stdin: str | None = None):
    """Run a command without ever letting a secret reach our output.

    Returns (returncode, stderr) or (None, reason) when the command could not complete.
    Exceptions are reduced to their type name: `TimeoutExpired` would otherwise carry the
    full command line.
    """
    try:
        proc = run(cmd, input=stdin, capture_output=True, text=True, timeout=60, env=env)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, type(exc).__name__
    return proc.returncode, proc.stderr or ""


def check_pat_secrets_write(env: dict[str, str], run: Runner) -> Result:
    """Write and delete a throwaway secret. Reading the public key only needs Secrets: Read,
    but the daily job must *write* ML_REFRESH_TOKEN, so prove write access here."""
    name = f"DATA_REPO_PAT can write Actions secrets of {MAIN_REPO}"
    pat = env.get("DATA_REPO_PAT")
    if not pat:
        return Result(name, False, "skipped: missing secret")
    gh_env = {**_base_env(env), "GH_TOKEN": pat}
    code, err = _run_safely(
        run,
        ["gh", "secret", "set", PROBE_SECRET_NAME, "--repo", MAIN_REPO],
        env=gh_env,
        stdin=secrets.token_hex(16),
    )
    if code != 0:
        reason = err if code is None else _http_hint(err.replace(pat, "***"))
        return Result(name, False, f"could not write a test secret ({reason}): "
                      "add 'Secrets: Read and write' for this repo")
    code, err = _run_safely(
        run, ["gh", "secret", "delete", PROBE_SECRET_NAME, "--repo", MAIN_REPO], env=gh_env
    )
    if code != 0:
        return Result(name, True, f"write OK, but delete the leftover {PROBE_SECRET_NAME} secret")
    return Result(name, True, "write access confirmed (test secret written and deleted)")


def check_data_repo_private(env: dict[str, str], http: httpx.Client) -> Result:
    name = f"{DATA_REPO} is private and reachable with DATA_REPO_PAT"
    pat = env.get("DATA_REPO_PAT")
    if not pat:
        return Result(name, False, "skipped: missing secret")
    try:
        resp = http.get(
            f"{GITHUB_API}/repos/{DATA_REPO}",
            headers={"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"},
        )
    except httpx.HTTPError as exc:
        return Result(name, False, f"network error: {type(exc).__name__}")
    if resp.status_code != 200:
        return Result(name, False, f"HTTP {resp.status_code}: give the PAT access to this repo")
    if resp.json().get("private") is not True:
        return Result(name, False, "the data repo is PUBLIC: make it private (ADR-003)")
    return Result(name, True, "private")


def check_pat_push(env: dict[str, str], run: Runner) -> Result:
    """`git push --dry-run` asks GitHub for the push endpoint, which requires write access,
    without sending data or updating refs. The PAT goes in an env-provided HTTP header,
    never in the URL or the command line."""
    name = f"DATA_REPO_PAT can push to {DATA_REPO}"
    pat = env.get("DATA_REPO_PAT")
    if not pat:
        return Result(name, False, "skipped: missing secret")
    basic = base64.b64encode(f"x-access-token:{pat}".encode()).decode()
    git_env = {
        **_base_env(env),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }
    url = f"https://github.com/{DATA_REPO}.git"
    code, err = _run_safely(
        run, ["git", "push", "--dry-run", url, "HEAD:refs/heads/check-secrets-dry-run"],
        env=git_env,
    )
    if code == 0:
        return Result(name, True, "Contents write access confirmed (dry run, nothing pushed)")
    reason = err if code is None else _http_hint(err.replace(pat, "***").replace(basic, "***"))
    return Result(name, False, f"git push --dry-run failed ({reason}): "
                  "add 'Contents: Read and write'")


def _base_env(env: dict[str, str]) -> dict[str, str]:
    """Minimal environment for child processes: no other secrets are passed on."""
    keep = ("PATH", "HOME", "LANG", "TMPDIR", "RUNNER_TEMP")
    return {k: env[k] for k in keep if k in env}


def _http_hint(stderr: str) -> str:
    low = stderr.lower()
    if "403" in low or "forbidden" in low:
        return "403 forbidden"
    if "404" in low or "not found" in low:
        return "404 not found"
    if "401" in low or "authentication" in low:
        return "401 unauthorized"
    return "error"


def main() -> int:
    env = dict(os.environ)
    results = check_format(env)
    with httpx.Client(timeout=20) as http:
        results.append(check_ml_client(env, http))
        results.append(check_data_repo_private(env, http))
    results.append(check_pat_secrets_write(env, subprocess.run))
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
