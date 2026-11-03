import subprocess

import httpx

from scripts import check_secrets as cs

GOOD = {
    "ML_CLIENT_ID": "1234567890123456",
    "ML_CLIENT_SECRET": "AbCdEfGhIjKlMnOpQrStUvWxYz012345",
    "ML_REFRESH_TOKEN": "TG-0123456789abcdef-123456",
    "DATA_REPO_PAT": "github_pat_" + "A" * 40,
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


def test_pat_secrets_permission():
    assert cs.check_pat_secrets(GOOD, client(lambda r: httpx.Response(200, json={}))).ok
    denied = cs.check_pat_secrets(GOOD, client(lambda r: httpx.Response(403)))
    assert not denied.ok and "Secrets" in denied.detail


def test_pat_push_redacts_token_and_reports_hint():
    def fake_run(cmd, **kwargs):
        stderr = f"fatal: unable to access {GOOD['DATA_REPO_PAT']} 403"
        return subprocess.CompletedProcess(cmd, 128, "", stderr)

    result = cs.check_pat_push(GOOD, fake_run)
    assert not result.ok
    assert GOOD["DATA_REPO_PAT"] not in result.detail
    assert "403" in result.detail


def test_pat_push_ok():
    result = cs.check_pat_push(GOOD, lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    assert result.ok
