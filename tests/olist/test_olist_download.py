import base64
import hashlib
import json
import logging
import zipfile
from pathlib import Path

import httpx
import pytest

from ingestion.olist import download
from ingestion.olist.download import (
    DownloadError,
    download_from_kaggle,
    extract_verified,
    main,
)
from ingestion.olist.files import EXPECTED_FILENAMES, MANIFEST_NAME, OLIST_FILES

FIXTURES = Path(__file__).parent / "fixtures"
USER, KEY = "someone", "fake-kaggle-key-123"


def make_zip(path: Path, *, extra: dict[str, bytes] | None = None, drop: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for spec in OLIST_FILES:
            if spec.filename != drop:
                zf.write(FIXTURES / spec.filename, spec.filename)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return path


def test_from_zip_extracts_nine_files_and_writes_manifest(tmp_path):
    archive = make_zip(tmp_path / "olist.zip")
    dest = tmp_path / "data" / "olist"

    assert main(["--dest", str(dest), "--from-zip", str(archive)]) == 0

    names = {p.name for p in dest.iterdir()}
    assert names == EXPECTED_FILENAMES | {MANIFEST_NAME}
    manifest = json.loads((dest / MANIFEST_NAME).read_text())
    assert manifest["dataset"] == "olistbr/brazilian-ecommerce"
    assert manifest["source"] == "zip"
    assert manifest["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    for spec in OLIST_FILES:
        data = (FIXTURES / spec.filename).read_bytes()
        assert (dest / spec.filename).read_bytes() == data
        assert manifest["files"][spec.filename] == {
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }


def test_rerun_replaces_files_and_leaves_no_temp_folders(tmp_path):
    archive = make_zip(tmp_path / "olist.zip")
    dest = tmp_path / "olist"
    dest.mkdir()
    (dest / "olist_orders_dataset.csv").write_text("stale")
    (dest / ".tmp-deadbeef").mkdir()  # leftover of a killed run
    (dest / ".download-deadbeef.zip").write_bytes(b"partial")

    assert main(["--dest", str(dest), "--from-zip", str(archive)]) == 0
    assert main(["--dest", str(dest), "--from-zip", str(archive)]) == 0

    assert (dest / "olist_orders_dataset.csv").read_bytes() == (
        FIXTURES / "olist_orders_dataset.csv"
    ).read_bytes()
    assert not [p for p in dest.iterdir() if p.name.startswith(".")]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"extra": {"notes.txt": b"x"}}, "unexpected entries ['notes.txt']"),
        ({"drop": "olist_sellers_dataset.csv"}, "missing files ['olist_sellers_dataset.csv']"),
        ({"extra": {"../evil.csv": b"x"}}, "unexpected entries ['../evil.csv']"),
        (
            {"extra": {"sub/olist_orders_dataset.csv": b"x"}},
            "unexpected entries ['sub/olist_orders_dataset.csv']",
        ),
    ],
)
def test_archive_must_contain_exactly_the_nine_csvs(tmp_path, kwargs, message):
    archive = make_zip(tmp_path / "olist.zip", **kwargs)
    dest = tmp_path / "olist"
    with pytest.raises(DownloadError) as err:
        extract_verified(archive, dest, source="zip")
    assert message in str(err.value)
    assert list(dest.iterdir()) == []  # nothing moved into place, temp folder removed


def test_duplicate_member_is_rejected(tmp_path):
    archive = make_zip(tmp_path / "olist.zip")
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        zipfile.ZipFile(archive, "a") as zf,
    ):
        zf.writestr("olist_orders_dataset.csv", b"order_id\n")
    with pytest.raises(DownloadError, match="duplicate entries"):
        extract_verified(archive, tmp_path / "olist", source="zip")


def test_not_a_zip_and_corrupt_member(tmp_path):
    bogus = tmp_path / "bogus.zip"
    bogus.write_bytes(b"<html>login required</html>")
    with pytest.raises(DownloadError, match="not a zip"):
        extract_verified(bogus, tmp_path / "a", source="zip")

    payload = (FIXTURES / "olist_customers_dataset.csv").read_bytes()
    # Stored (uncompressed) copy so we can flip a byte of the member data and break its CRC.
    stored = tmp_path / "stored.zip"
    with zipfile.ZipFile(stored, "w", compression=zipfile.ZIP_STORED) as zf:
        for spec in OLIST_FILES:
            zf.write(FIXTURES / spec.filename, spec.filename)
    raw = bytearray(stored.read_bytes())
    idx = raw.index(payload[:40])
    raw[idx + 5] ^= 0xFF
    stored.write_bytes(bytes(raw))
    with pytest.raises(DownloadError, match="corrupt archive"):
        extract_verified(stored, tmp_path / "b", source="zip")


def test_missing_zip_and_missing_credentials_are_config_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    assert main(["--dest", str(tmp_path / "o"), "--from-zip", str(tmp_path / "nope.zip")]) == 2
    assert main(["--dest", str(tmp_path / "o")]) == 2


def test_invalid_archive_exit_code(tmp_path):
    archive = make_zip(tmp_path / "olist.zip", drop="olist_orders_dataset.csv")
    assert main(["--dest", str(tmp_path / "o"), "--from-zip", str(archive)]) == 1


# --- Kaggle API (mocked HTTP) ---------------------------------------------------------------


class Sleeps(list):
    def __call__(self, seconds):
        self.append(seconds)


def test_kaggle_basic_auth_redirect_drops_auth_and_log_hides_secrets(tmp_path, caplog):
    zip_bytes = make_zip(tmp_path / "src.zip").read_bytes()
    seen: list[httpx.Request] = []
    signed = "https://storage.googleapis.com/bucket/archive.zip?X-Goog-Signature=SECRETSIG"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "www.kaggle.com":
            return httpx.Response(302, headers={"Location": signed})
        return httpx.Response(200, content=zip_bytes)

    target = tmp_path / "dl.zip"
    download.silence_http_logging()  # what main() does
    with caplog.at_level(logging.INFO):
        download_from_kaggle(
            USER, KEY, target, http=httpx.Client(transport=httpx.MockTransport(handler))
        )

    assert target.read_bytes() == zip_bytes
    first, second = seen
    assert first.url.path == "/api/v1/datasets/download/olistbr/brazilian-ecommerce"
    expected = "Basic " + base64.b64encode(f"{USER}:{KEY}".encode()).decode()
    assert first.headers["Authorization"] == expected
    assert KEY not in str(first.url)
    assert "Authorization" not in second.headers  # cross-origin redirect: credentials dropped
    assert KEY not in caplog.text and "SECRETSIG" not in caplog.text
    assert "storage.googleapis.com" in caplog.text


def test_kaggle_retries_429_and_5xx_with_backoff(tmp_path):
    zip_bytes = make_zip(tmp_path / "src.zip").read_bytes()
    responses = [httpx.Response(429), httpx.Response(503), httpx.Response(200, content=zip_bytes)]

    def handler(request):
        return responses.pop(0)

    sleeps = Sleeps()
    download_from_kaggle(
        USER,
        KEY,
        tmp_path / "dl.zip",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps,
    )
    assert sleeps == [2.0, 4.0]


def test_kaggle_network_errors_exhaust_retries(tmp_path):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    sleeps = Sleeps()
    with pytest.raises(DownloadError, match="after 4 attempts"):
        download_from_kaggle(
            USER,
            KEY,
            tmp_path / "dl.zip",
            http=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=sleeps,
        )
    assert sleeps == [2.0, 4.0, 8.0]


@pytest.mark.parametrize(("status", "match"), [(401, "KAGGLE_USERNAME"), (404, "HTTP 404")])
def test_kaggle_client_errors_are_not_retried(tmp_path, status, match):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    with pytest.raises(DownloadError, match=match):
        download_from_kaggle(
            USER,
            KEY,
            tmp_path / "dl.zip",
            http=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=Sleeps(),
        )
    assert len(calls) == 1


def test_main_kaggle_mode_extracts_and_removes_the_archive(tmp_path, monkeypatch):
    zip_bytes = make_zip(tmp_path / "src.zip").read_bytes()
    monkeypatch.setenv("KAGGLE_USERNAME", USER)
    monkeypatch.setenv("KAGGLE_KEY", KEY)
    real = download.download_from_kaggle

    def fake(username, key, target, **kwargs):
        client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=zip_bytes))
        )
        real(username, key, target, http=client, **kwargs)

    monkeypatch.setattr(download, "download_from_kaggle", fake)
    dest = tmp_path / "olist"
    assert main(["--dest", str(dest)]) == 0
    manifest = json.loads((dest / MANIFEST_NAME).read_text())
    assert manifest["source"] == "kaggle-api"
    assert not list(dest.glob(".download-*"))
