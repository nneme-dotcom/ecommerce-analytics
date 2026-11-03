"""Download the Olist dataset and unpack it, verified, into a local folder.

    python -m ingestion.olist.download --dest data/olist                    # Kaggle API
    python -m ingestion.olist.download --dest data/olist --from-zip <zip>   # manual download

Kaggle mode needs ``KAGGLE_USERNAME`` and ``KAGGLE_KEY`` (kaggle.com → Settings → API). They are
sent as HTTP Basic auth to the documented download endpoint and never logged. Kaggle redirects to
a signed storage URL: httpx drops the ``Authorization`` header on a cross-origin redirect, and only
the host of that URL is ever logged (its query string carries a signature; httpx's own request log
is raised to WARNING for that reason).

The zip must contain exactly the nine expected CSVs (DATA_CONTRACT §2), at its root. Files are
extracted to a temp folder inside ``--dest`` and moved into place only after every file passed,
then ``manifest.json`` (SHA-256 and size per file) is written next to them. Re-running replaces
the files.

Exit codes: 0 ok · 1 download or validation failed · 2 configuration error (no credentials, no zip).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import time
import uuid
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ingestion.olist.files import EXPECTED_FILENAMES, KAGGLE_DATASET, MANIFEST_NAME, OLIST_FILES

KAGGLE_DOWNLOAD_URL = f"https://www.kaggle.com/api/v1/datasets/download/{KAGGLE_DATASET}"
# The real archive is ~45 MB zipped / ~130 MB unzipped; anything far bigger is not the dataset.
MAX_UNCOMPRESSED_BYTES = 2 * 1024**3
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_S = 2.0
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=120.0)
CHUNK = 1024 * 1024

EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2

logger = logging.getLogger(__name__)


class DownloadError(RuntimeError):
    """The archive could not be downloaded or is not the expected dataset."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _retryable(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def download_from_kaggle(
    username: str,
    key: str,
    target: Path,
    *,
    url: str = KAGGLE_DOWNLOAD_URL,
    http: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
) -> None:
    """Stream the dataset zip to ``target``. Retries 429/5xx/network errors with backoff."""
    client = http or httpx.Client(timeout=DOWNLOAD_TIMEOUT)
    try:
        for attempt in range(max_retries + 1):
            if attempt:
                delay = backoff_base_s * 2 ** (attempt - 1)
                logger.info("retrying Kaggle download in %.1fs (attempt %d)", delay, attempt + 1)
                sleep(delay)
            try:
                with client.stream("GET", url, auth=(username, key), follow_redirects=True) as resp:
                    host = resp.url.host  # never log the full (signed) URL
                    if resp.status_code == 200:
                        with target.open("wb") as fh:
                            for chunk in resp.iter_bytes(CHUNK):
                                fh.write(chunk)
                        logger.info("downloaded %d bytes from %s", target.stat().st_size, host)
                        return
                    logger.warning("Kaggle download: HTTP %d from %s", resp.status_code, host)
                    if resp.status_code in (401, 403):
                        raise DownloadError(
                            f"Kaggle refused the download (HTTP {resp.status_code}): check "
                            "KAGGLE_USERNAME / KAGGLE_KEY, or download the zip manually and use "
                            "--from-zip"
                        )
                    if not _retryable(resp.status_code):
                        raise DownloadError(f"Kaggle download failed (HTTP {resp.status_code})")
            except httpx.HTTPError as exc:
                logger.warning("Kaggle download: network error (%s)", type(exc).__name__)
        raise DownloadError(f"Kaggle download failed after {max_retries + 1} attempts")
    finally:
        if http is None:
            client.close()


def validate_members(zf: zipfile.ZipFile) -> None:
    """The archive must hold exactly the nine expected CSVs, at its root, nothing else."""
    names = [info.filename for info in zf.infolist()]
    problems: list[str] = []
    unexpected = sorted(n for n in names if n not in EXPECTED_FILENAMES)
    missing = sorted(EXPECTED_FILENAMES - set(names))
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if unexpected:
        problems.append(f"unexpected entries {unexpected}")
    if missing:
        problems.append(f"missing files {missing}")
    if duplicates:
        problems.append(f"duplicate entries {duplicates}")
    total = sum(info.file_size for info in zf.infolist())
    if total > MAX_UNCOMPRESSED_BYTES:
        problems.append(f"uncompressed size {total} bytes exceeds {MAX_UNCOMPRESSED_BYTES}")
    if problems:
        raise DownloadError("not the Olist dataset archive: " + "; ".join(problems))


def extract_verified(zip_path: Path, dest: Path, *, source: str) -> dict[str, Any]:
    """Validate ``zip_path``, extract the nine CSVs into ``dest`` and write the manifest."""
    if not zipfile.is_zipfile(zip_path):
        raise DownloadError("the file is not a zip archive")
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / f".tmp-{uuid.uuid4().hex}"
    tmp.mkdir()
    try:
        files: dict[str, dict[str, Any]] = {}
        with zipfile.ZipFile(zip_path) as zf:
            validate_members(zf)
            for spec in OLIST_FILES:
                digest = hashlib.sha256()
                size = 0
                # Only known names are written, to a path we build: no zip-slip possible.
                # Reading to the end makes zipfile verify the member's CRC-32.
                with zf.open(spec.filename) as src, (tmp / spec.filename).open("wb") as out:
                    while chunk := src.read(CHUNK):
                        digest.update(chunk)
                        size += len(chunk)
                        out.write(chunk)
                files[spec.filename] = {"sha256": digest.hexdigest(), "bytes": size}
        manifest = {
            "dataset": KAGGLE_DATASET,
            "source": source,
            "archive_sha256": sha256_file(zip_path),
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "files": files,
        }
        (tmp / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        for spec in OLIST_FILES:
            os.replace(tmp / spec.filename, dest / spec.filename)
        os.replace(tmp / MANIFEST_NAME, dest / MANIFEST_NAME)  # last: marks a complete set
        return manifest
    except zipfile.BadZipFile as exc:
        raise DownloadError(f"corrupt archive: {exc}") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cleanup_stale(dest: Path) -> None:
    """Remove leftovers of a killed run (temp folders, partial downloads)."""
    if not dest.is_dir():
        return
    for path in dest.glob(".tmp-*"):
        shutil.rmtree(path, ignore_errors=True)
    for path in dest.glob(".download-*.zip"):
        path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ingestion.olist.download",
        description="Download (Kaggle API) or unpack (--from-zip) the Olist dataset, verified.",
    )
    parser.add_argument("--dest", type=Path, default=Path("data/olist"), help="target folder")
    parser.add_argument(
        "--from-zip", type=Path, help="a manually downloaded archive (skips the Kaggle API)"
    )
    return parser


def silence_http_logging() -> None:
    """httpx logs every request URL at INFO; Kaggle's redirect target is a signed URL."""
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    silence_http_logging()
    args = build_parser().parse_args(argv)
    dest: Path = args.dest
    cleanup_stale(dest)
    try:
        if args.from_zip:
            if not args.from_zip.is_file():
                logger.error("--from-zip: file not found")
                return EXIT_CONFIG
            manifest = extract_verified(args.from_zip, dest, source="zip")
        else:
            username = os.environ.get("KAGGLE_USERNAME", "").strip()
            key = os.environ.get("KAGGLE_KEY", "").strip()
            if not (username and key):
                logger.error(
                    "set KAGGLE_USERNAME and KAGGLE_KEY, or pass --from-zip <path> with the zip "
                    "downloaded from https://www.kaggle.com/datasets/%s",
                    KAGGLE_DATASET,
                )
                return EXIT_CONFIG
            dest.mkdir(parents=True, exist_ok=True)
            archive = dest / f".download-{uuid.uuid4().hex}.zip"
            try:
                download_from_kaggle(username, key, archive)
                manifest = extract_verified(archive, dest, source="kaggle-api")
            finally:
                archive.unlink(missing_ok=True)
    except DownloadError as exc:
        logger.error("%s", exc)
        return EXIT_FAILED
    logger.info("wrote %d files and %s to %s", len(manifest["files"]), MANIFEST_NAME, dest)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
