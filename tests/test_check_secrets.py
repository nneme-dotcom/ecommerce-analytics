import subprocess

import httpx

from scripts import check_secrets as cs

GOOD = {
    "ML_CLIENT_ID": "1234567890123456",
    "ML_CLIENT_SECRET": "fakeclientsecretfortests",
    "ML_REFRESH_TOKEN": "TG-fake-refresh-token-for-tests",
    "DATA_REPO_PAT": "github_pat_" + "x" * 40,
}


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_formats_pass_for_well_formed_values():
    assert all(r.ok for r in cs.check_format(GOOD))


def test_missing_and_malformed_values_fail_without_echoing_them():
    env = dict(
        GOOD,
        ML_CLIENT_ID="",
        ML_REFRESH_TOKEN="APP_USR-oops",
        DATA_REPO_PAT=" " + GOOD["DATA_REPO_PAT"],
    )
    results = {r.name.split()[0]: r for r in cs.check_format(env)}
    assert not results["ML_CLIENT_ID"].ok
    assert not results["ML_REFRESH_TOKEN"].ok
    assert "spaces" in results["DATA_REPO_PAT"].detail
    assert all("APP_USR-oops" not in r.detail for r in results.values())


def test_ml_client_valid_when_only_the_fake_refresh_token_is_rejected():
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        return httpx.Response(400, json={"error": "invalid_grant"})

    result = cs.check_ml_client(GOOD, client(handler))
    assert result.ok
    assert cs.PROBE_REFRESH_TOKEN in seen["body"]
    assert GOOD["ML_REFRESH_TOKEN"] not in seen["body"]  # the real token is never spent


def test_ml_client_invalid_on_client_error():
    resp = httpx.Response(400, json={"error": "invalid_client"})
    result = cs.check_ml_client(GOOD, client(lambda r: resp))
    assert not result.ok and "invalid_client" in result.detail


def runner(responses):
    """Fake subprocess.run returning (returncode, stderr) per call; records the calls."""
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        code, stderr = responses[len(calls) - 1]
        if isinstance(code, BaseException):
            raise code
        return subprocess.CompletedProcess(cmd, code, "", stderr)

    run.calls = calls
    return run


def test_secrets_write_probe_writes_then_deletes_a_throwaway_secret():
    run = runner([(0, ""), (0, "")])
    result = cs.check_pat_secrets_write(GOOD, run)
    assert result.ok
    (set_cmd, set_kw), (del_cmd, _) = run.calls
    assert set_cmd[:4] == ["gh", "secret", "set", cs.PROBE_SECRET_NAME]
    assert del_cmd[:4] == ["gh", "secret", "delete", cs.PROBE_SECRET_NAME]
    assert "ML_REFRESH_TOKEN" not in " ".join(set_cmd + del_cmd)
    assert set_kw["env"]["GH_TOKEN"] == GOOD["DATA_REPO_PAT"]
    assert "ML_CLIENT_SECRET" not in set_kw["env"]  # no other secret leaks into the child
    assert GOOD["DATA_REPO_PAT"] not in " ".join(set_cmd)


def test_secrets_read_only_pat_fails():
    result = cs.check_pat_secrets_write(GOOD, runner([(1, "HTTP 403: Resource not accessible")]))
    assert not result.ok and "403" in result.detail and "Secrets" in result.detail


def test_data_repo_must_be_private():
    ok = cs.check_data_repo_private(
        GOOD, client(lambda r: httpx.Response(200, json={"private": True}))
    )
    public = cs.check_data_repo_private(
        GOOD, client(lambda r: httpx.Response(200, json={"private": False}))
    )
    assert ok.ok
    assert not public.ok and "PUBLIC" in public.detail


def test_pat_push_uses_header_not_url_and_is_dry_run():
    run = runner([(0, "")])
    assert cs.check_pat_push(GOOD, run).ok
    cmd, kwargs = run.calls[0]
    assert "--dry-run" in cmd
    assert all(GOOD["DATA_REPO_PAT"] not in part for part in cmd)
    assert kwargs["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_pat_push_failure_is_redacted():
    stderr = f"remote: denied for {GOOD['DATA_REPO_PAT']}\nfatal: ... 403"
    result = cs.check_pat_push(GOOD, runner([(128, stderr)]))
    assert not result.ok and "403" in result.detail
    assert GOOD["DATA_REPO_PAT"] not in result.detail


def test_timeout_never_exposes_the_command():
    timeout = subprocess.TimeoutExpired(["git", "push", GOOD["DATA_REPO_PAT"]], 60)
    result = cs.check_pat_push(GOOD, runner([(timeout, "")]))
    assert not result.ok
    assert "TimeoutExpired" in result.detail
    assert GOOD["DATA_REPO_PAT"] not in result.detail
