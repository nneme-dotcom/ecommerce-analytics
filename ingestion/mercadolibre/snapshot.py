"""Daily Mercado Libre snapshot: best sellers -> catalog products -> every offer, plus trends.

    python -m ingestion.mercadolibre.snapshot --out <storage-root> \\
        --new-refresh-token-file <path> [--date YYYY-MM-DD] [--categories MLA1055 ...]

Environment: ``ML_CLIENT_ID``, ``ML_CLIENT_SECRET``, ``ML_REFRESH_TOKEN``.

Output layout and fields: docs/DATA_CONTRACT.md section 1. The day's folder is replaced atomically,
so re-running a date overwrites it. A ``failed`` run never replaces an existing usable snapshot.

The refresh token rotates on every use (ADR-004): the new one is written to
``--new-refresh-token-file`` immediately after the refresh succeeds, before any data request.

Exit codes: 0 success/partial · 1 failed · 2 config or token refresh error · 3 could not persist
the new refresh token.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import shutil
import sys
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from ingestion.mercadolibre.auth import AuthError, ReauthorizationRequired, refresh_access_token
from ingestion.mercadolibre.client import (
    API_BASE_URL,
    DEFAULT_DELAY_S,
    MercadoLibreClient,
    RequestCapReached,
)

SITE_ID = "MLA"
# MLA1055 (cell phones) is verified in docs/API_PROBE.md. The other four were picked for variety
# and must be validated against /sites/MLA/categories in the first live run (DATA_CONTRACT §1).
DEFAULT_CATEGORIES = ("MLA1055", "MLA1652", "MLA1002", "MLA109027", "MLA5726")
DEFAULT_MAX_REQUESTS = 1000
DEFAULT_MAX_OFFERS_PER_PRODUCT = 100
OFFERS_PAGE_SIZE = 50

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_AUTH = 2
EXIT_TOKEN_PERSIST = 3

FILES = ("highlights", "products", "offers", "trends")

logger = logging.getLogger(__name__)


class TokenPersistError(RuntimeError):
    """The rotated refresh token could not be written. The next run would fail (ADR-004)."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- record mappers
# Field sources follow DATA_CONTRACT §1. Fields not confirmed by docs/API_PROBE.md are read with
# .get() and may be null.


def _attribute(product: dict[str, Any], attr_id: str) -> str | None:
    for attr in product.get("attributes") or []:
        if isinstance(attr, dict) and attr.get("id") == attr_id:
            return attr.get("value_name")
    return None


def map_highlights(category_id: str, payload: Any) -> list[dict[str, Any]]:
    content = payload.get("content") if isinstance(payload, dict) else None
    rows = []
    for entry in content or []:
        if not isinstance(entry, dict) or entry.get("type") != "PRODUCT" or not entry.get("id"):
            continue
        rows.append(
            {
                "category_id": category_id,
                "position": entry.get("position"),
                "product_id": entry["id"],
            }
        )
    return rows


def map_product(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "product_id": payload.get("id"),
        "name": payload.get("name"),
        "domain_id": payload.get("domain_id"),
        "family_name": payload.get("family_name"),
        "status": payload.get("status"),
        "brand": _attribute(payload, "BRAND"),
        "model": _attribute(payload, "MODEL"),
        "permalink": payload.get("permalink"),
    }


def map_offer(product_id: str, offer: dict[str, Any]) -> dict[str, Any]:
    shipping = offer.get("shipping") or {}
    seller_address = offer.get("seller_address") or {}
    state = seller_address.get("state") if isinstance(seller_address, dict) else None
    return {
        "product_id": product_id,
        "item_id": offer.get("item_id"),
        "seller_id": offer.get("seller_id"),
        "official_store_id": offer.get("official_store_id"),
        "price": offer.get("price"),
        "original_price": offer.get("original_price"),
        "currency_id": offer.get("currency_id"),
        "condition": offer.get("condition"),
        "listing_type_id": offer.get("listing_type_id"),
        "category_id": offer.get("category_id"),
        "free_shipping": shipping.get("free_shipping"),
        "logistic_type": shipping.get("logistic_type"),
        "shipping_mode": shipping.get("mode"),
        "deal_ids": offer.get("deal_ids") or [],
        "tags": offer.get("tags") or [],
        "seller_state": state.get("name") if isinstance(state, dict) else None,
    }


def map_trends(payload: Any) -> list[dict[str, Any]]:
    rows = []
    for index, entry in enumerate(payload if isinstance(payload, list) else [], start=1):
        if isinstance(entry, dict):
            rows.append(
                {"position": index, "keyword": entry.get("keyword"), "url": entry.get("url")}
            )
    return rows


# --------------------------------------------------------------------------- collection


@dataclass
class Collected:
    run_id: str
    snapshot_date: str
    rows: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {f: [] for f in FILES})
    cap_reached: bool = False

    def add(self, kind: str, records: Iterable[dict[str, Any]], captured_at: str) -> None:
        meta = {
            "snapshot_date": self.snapshot_date,
            "run_id": self.run_id,
            "captured_at": captured_at,
        }
        self.rows[kind].extend({**meta, **r} for r in records)


def _fetch_offers(
    client: MercadoLibreClient, product_id: str, max_offers: int
) -> tuple[list[dict[str, Any]], str] | None:
    """All offers of one catalog product, paginated, capped at ``max_offers``."""
    path = f"/products/{product_id}/items"
    offers: list[dict[str, Any]] = []
    captured_at: str | None = None
    offset = 0
    while len(offers) < max_offers:
        limit = min(OFFERS_PAGE_SIZE, max_offers - len(offers))
        payload = client.get(path, params={"offset": offset, "limit": limit})
        if not isinstance(payload, dict):
            # First page failed: no offers for this product. Later page failed: keep what we have.
            break
        captured_at = captured_at or _now()
        results = [r for r in (payload.get("results") or []) if isinstance(r, dict)]
        offers.extend(results)
        total = (payload.get("paging") or {}).get("total")
        offset += len(results)
        if not results or not isinstance(total, int) or offset >= total:
            break
    if captured_at is None:
        return None
    return offers[:max_offers], captured_at


def collect(
    client: MercadoLibreClient,
    categories: Iterable[str],
    run_id: str,
    snapshot_date: str,
    max_offers_per_product: int = DEFAULT_MAX_OFFERS_PER_PRODUCT,
) -> Collected:
    out = Collected(run_id=run_id, snapshot_date=snapshot_date)
    seen_products: set[str] = set()
    try:
        trends = client.get(f"/trends/{SITE_ID}")
        if trends is not None:
            out.add("trends", map_trends(trends), _now())

        for category_id in categories:
            payload = client.get(f"/highlights/{SITE_ID}/category/{category_id}")
            if payload is None:
                continue
            highlights = map_highlights(category_id, payload)
            out.add("highlights", highlights, _now())
            logger.info("category %s: %d ranked products", category_id, len(highlights))

            for row in highlights:
                product_id = row["product_id"]
                if product_id in seen_products:
                    continue
                seen_products.add(product_id)
                product = client.get(f"/products/{product_id}")
                if isinstance(product, dict):
                    out.add("products", [map_product(product)], _now())
                fetched = _fetch_offers(client, product_id, max_offers_per_product)
                if fetched is not None:
                    offers, captured_at = fetched
                    out.add("offers", (map_offer(product_id, o) for o in offers), captured_at)
    except RequestCapReached:
        out.cap_reached = True
        logger.warning("request cap reached; writing what was collected so far")
    return out


def decide_status(collected: Collected, errors: list[dict[str, Any]]) -> str:
    if not collected.rows["highlights"]:
        return "failed"
    if errors or collected.cap_reached:
        return "partial"
    return "success"


# --------------------------------------------------------------------------- writing


def _write_jsonl_gz(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        for row in rows:
            gz.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            gz.write(b"\n")


def _existing_is_usable(day_dir: Path) -> bool:
    try:
        meta = json.loads((day_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return meta.get("status") in ("success", "partial")


def write_snapshot(
    out_root: Path, snapshot_date: str, run_meta: dict[str, Any], collected: Collected
) -> Path | None:
    """Write the day folder atomically. Returns the folder, or None if it was left untouched."""
    base = out_root / "mercadolibre"
    day_dir = base / f"snapshot_date={snapshot_date}"
    if run_meta["status"] == "failed" and _existing_is_usable(day_dir):
        logger.warning("run failed; keeping the existing usable snapshot for %s", snapshot_date)
        return None

    base.mkdir(parents=True, exist_ok=True)
    tmp_dir = base / f".tmp-{snapshot_date}-{run_meta['run_id']}"
    old_dir = base / f".old-{snapshot_date}-{run_meta['run_id']}"
    tmp_dir.mkdir()
    try:
        for kind in FILES:
            _write_jsonl_gz(tmp_dir / f"{kind}.jsonl.gz", collected.rows[kind])
        (tmp_dir / "run.json").write_text(
            json.dumps(run_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        # Directory renames are atomic on the same filesystem. os.replace cannot replace a
        # non-empty directory, so move the old folder aside first, then swap in the new one.
        if day_dir.exists():
            day_dir.rename(old_dir)
        try:
            tmp_dir.rename(day_dir)
        except OSError:
            if old_dir.exists():
                old_dir.rename(day_dir)
            raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(old_dir, ignore_errors=True)
    return day_dir


def persist_refresh_token(path: Path, token: str) -> None:
    """Write the rotated refresh token (owner-only permissions where supported)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
        os.replace(tmp, path)
    except OSError as exc:
        raise TokenPersistError(
            f"could not write the new refresh token: {type(exc).__name__}"
        ) from None


# --------------------------------------------------------------------------- orchestration


def run(
    *,
    out_root: Path,
    snapshot_date: str,
    categories: list[str],
    new_refresh_token_file: Path,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    delay_s: float = DEFAULT_DELAY_S,
    max_offers_per_product: int = DEFAULT_MAX_OFFERS_PER_PRODUCT,
    http: httpx.Client | None = None,
    base_url: str = API_BASE_URL,
    sleep: Callable[[float], None] | None = None,
) -> int:
    run_id = str(uuid.uuid4())
    started_at = _now()

    try:
        tokens = refresh_access_token(client_id, client_secret, refresh_token, http=http)
    except ReauthorizationRequired as exc:
        logger.error("%s", exc)
        return EXIT_AUTH
    except AuthError as exc:
        logger.error("token refresh failed: %s", exc)
        return EXIT_AUTH

    # Persist the rotated token before anything else can fail (ADR-004).
    try:
        persist_refresh_token(new_refresh_token_file, tokens.refresh_token)
    except TokenPersistError as exc:
        logger.error("%s; the owner will need to re-authorize", exc)
        return EXIT_TOKEN_PERSIST
    logger.info("new refresh token persisted")

    client_kwargs: dict[str, Any] = {
        "base_url": base_url,
        "delay_s": delay_s,
        "max_requests": max_requests,
        "http": http,
    }
    if sleep is not None:
        client_kwargs["sleep"] = sleep
    with MercadoLibreClient(tokens.access_token, **client_kwargs) as client:
        collected = collect(client, categories, run_id, snapshot_date, max_offers_per_product)
        errors = client.error_summary()
        requests_made = client.requests_made

    status = decide_status(collected, errors)
    run_meta = {
        "run_id": run_id,
        "snapshot_date": snapshot_date,
        "started_at": started_at,
        "finished_at": _now(),
        "site_id": SITE_ID,
        "categories": list(categories),
        "requests_made": requests_made,
        "request_cap": max_requests,
        "request_cap_reached": collected.cap_reached,
        "row_counts": {kind: len(collected.rows[kind]) for kind in FILES},
        "errors": errors,
        "status": status,
    }
    write_snapshot(out_root, snapshot_date, run_meta, collected)
    logger.info(
        "snapshot %s status=%s requests=%d rows=%s errors=%d",
        snapshot_date,
        status,
        requests_made,
        run_meta["row_counts"],
        len(errors),
    )
    return EXIT_FAILED if status == "failed" else EXIT_OK


def _parse_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ingestion.mercadolibre.snapshot")
    parser.add_argument("--out", required=True, type=Path, help="storage root (ADR-003)")
    parser.add_argument("--date", type=_parse_date, default=None, help="default: today (UTC)")
    parser.add_argument("--categories", nargs="+", default=list(DEFAULT_CATEGORIES))
    parser.add_argument(
        "--new-refresh-token-file",
        required=True,
        type=Path,
        help="where to write the rotated refresh token (outside the repo)",
    )
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_S)
    parser.add_argument(
        "--max-offers-per-product", type=int, default=DEFAULT_MAX_OFFERS_PER_PRODUCT
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    missing = [
        n for n in ("ML_CLIENT_ID", "ML_CLIENT_SECRET", "ML_REFRESH_TOKEN") if not os.environ.get(n)
    ]
    if missing:
        logger.error("missing environment variables: %s", ", ".join(missing))
        return EXIT_AUTH
    return run(
        out_root=args.out,
        snapshot_date=args.date or datetime.now(UTC).date().isoformat(),
        categories=args.categories,
        new_refresh_token_file=args.new_refresh_token_file,
        client_id=os.environ["ML_CLIENT_ID"],
        client_secret=os.environ["ML_CLIENT_SECRET"],
        refresh_token=os.environ["ML_REFRESH_TOKEN"],
        max_requests=args.max_requests,
        delay_s=args.delay,
        max_offers_per_product=args.max_offers_per_product,
    )


if __name__ == "__main__":
    sys.exit(main())
