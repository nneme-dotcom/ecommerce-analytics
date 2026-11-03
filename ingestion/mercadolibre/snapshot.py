"""Daily Mercado Libre snapshot: best sellers -> catalog products -> every offer, plus trends.

    python -m ingestion.mercadolibre.snapshot --out <storage-root> \\
        --new-refresh-token-file <path> [--date YYYY-MM-DD] [--categories MLA1055 ...]

Environment: ``ML_CLIENT_ID``, ``ML_CLIENT_SECRET``, ``ML_REFRESH_TOKEN``.

Output layout and fields: docs/DATA_CONTRACT.md section 1. The day's folder is replaced by rename,
so re-running a date overwrites it, subject to the replacement rules in the contract (a run never
replaces a better one: success > partial > failed).

The refresh token rotates on every use (ADR-004): the new one is written to
``--new-refresh-token-file`` immediately after the refresh succeeds, before any data request.

Exit codes (see EXIT_* below and CLAUDE.md):
  0 success or partial · 1 failed · 2 re-authorization required · 3 new refresh token not persisted
  4 token endpoint unavailable after retries (transient) · 5 configuration error
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
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from ingestion.mercadolibre.auth import (
    AuthError,
    ReauthorizationRequired,
    TransientAuthError,
    refresh_access_token,
)
from ingestion.mercadolibre.client import (
    API_BASE_URL,
    DEFAULT_DELAY_S,
    NOT_FOUND,
    MercadoLibreClient,
    RequestCapReached,
)

SITE_ID = "MLA"
# All five verified live on 2026-11-03 (20 highlights each; docs/API_PROBE.md).
DEFAULT_CATEGORIES = ("MLA1055", "MLA1652", "MLA1002", "MLA109027", "MLA5726")
DEFAULT_MAX_REQUESTS = 1000
DEFAULT_MAX_OFFERS_PER_PRODUCT = 100
OFFERS_PAGE_SIZE = 50  # verified: limit=50 returns paging {limit: 50, offset, total}

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REAUTHORIZE = 2
EXIT_TOKEN_PERSIST = 3
EXIT_TOKEN_TRANSIENT = 4
EXIT_CONFIG = 5

FILES = ("highlights", "products", "offers", "trends")
FOLLOWED_TYPE = "PRODUCT"
STATUS_RANK = {"failed": 0, "partial": 1, "success": 2}
STORAGE_GITIGNORE_PATTERNS = (".tmp-*/", ".old-*/")

logger = logging.getLogger(__name__)


class TokenPersistError(RuntimeError):
    """The rotated refresh token could not be written. The next run would fail (ADR-004)."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- record mappers
# Field sources follow DATA_CONTRACT §1. Every mapper tolerates malformed shapes: a value of the
# wrong container type becomes null (scalars/objects) or [] (lists); it never raises.


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str_list(value: Any) -> list[str]:
    return [v for v in _list(value) if isinstance(v, str)]


def _attribute(product: dict[str, Any], attr_id: str) -> str | None:
    for attr in _list(product.get("attributes")):
        if isinstance(attr, dict) and attr.get("id") == attr_id:
            value = attr.get("value_name")
            return value if isinstance(value, str) else None
    return None


@dataclass
class HighlightsResult:
    rows: list[dict[str, Any]]
    skipped: Counter[str]  # per-type count of entries not followed (and MALFORMED drops)


def map_highlights(category_id: str, payload: Any) -> HighlightsResult:
    """All ranked entries (any ``type``), deduplicated per (category, id)."""
    rows: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    seen: set[str] = set()
    for entry in _list(_dict(payload).get("content")):
        entry_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(entry_id, str) or not entry_id:
            skipped["MALFORMED"] += 1
            continue
        if entry_id in seen:
            continue
        seen.add(entry_id)
        entry_type = entry.get("type") if isinstance(entry.get("type"), str) else None
        position = entry.get("position")
        rows.append(
            {
                "category_id": category_id,
                "position": position if isinstance(position, int) else None,
                "product_id": entry_id,
                "type": entry_type,
            }
        )
        if entry_type != FOLLOWED_TYPE:
            skipped[entry_type or "UNKNOWN"] += 1
    return HighlightsResult(rows, skipped)


def map_product(product_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    payload_id = payload.get("id")
    return {
        "product_id": payload_id if isinstance(payload_id, str) else product_id,
        "name": payload.get("name"),
        "domain_id": payload.get("domain_id"),
        "family_name": payload.get("family_name"),
        "status": payload.get("status"),
        "brand": _attribute(payload, "BRAND"),
        "model": _attribute(payload, "MODEL"),
        "permalink": payload.get("permalink"),
    }


def map_offer(product_id: str, offer: dict[str, Any]) -> dict[str, Any]:
    shipping = _dict(offer.get("shipping"))
    state = _dict(_dict(offer.get("seller_address")).get("state"))
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
        "deal_ids": _str_list(offer.get("deal_ids")),
        "tags": _str_list(offer.get("tags")),
        "seller_state": state.get("name"),
    }


def map_trends(payload: Any) -> list[dict[str, Any]]:
    rows = []
    for index, entry in enumerate(_list(payload), start=1):
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
    categories_captured: list[str] = field(default_factory=list)
    skipped_highlights: Counter[str] = field(default_factory=Counter)
    products_without_offers: int = 0
    cap_reached: bool = False

    def add(self, kind: str, records: Iterable[dict[str, Any]], captured_at: str) -> None:
        meta = {
            "snapshot_date": self.snapshot_date,
            "run_id": self.run_id,
            "captured_at": captured_at,
        }
        self.rows[kind].extend({**meta, **r} for r in records)


@dataclass
class OffersResult:
    offers: list[dict[str, Any]]
    captured_at: str | None
    cap_reached: bool = False
    no_offers: bool = False  # the product has no offers (first page 404, or total 0 / empty)


def fetch_offers(client: MercadoLibreClient, product_id: str, max_offers: int) -> OffersResult:
    """Offers of one catalog product: paginated, deduplicated by ``item_id``, capped.

    Never requests ``offset >= paging.total`` (the API answers 404). A 404 on the first page means
    "no offers" (e.g. out of stock) and a 404 on a later page means end of data; neither is an
    error. Stops when a page adds no new ``item_id``. A failed later page keeps the offers already
    fetched. If the request cap is hit mid-pagination, the offers fetched so far are kept and
    ``cap_reached`` is set.
    """
    path = f"/products/{product_id}/items"
    offers: list[dict[str, Any]] = []
    seen_items: set[Any] = set()
    captured_at: str | None = None
    offset = 0
    total: int | None = None
    try:
        while len(offers) < max_offers and (total is None or offset < total):
            limit = min(OFFERS_PAGE_SIZE, max_offers - len(offers))
            payload = client.get(path, params={"offset": offset, "limit": limit}, not_found_ok=True)
            if payload is NOT_FOUND:
                if offset == 0:
                    return OffersResult([], None, no_offers=True)
                break
            if not isinstance(payload, dict):
                if payload is not None:
                    client.record_error(path, None)  # 200 with a non-object body
                break
            captured_at = captured_at or _now()
            results = _list(payload.get("results"))
            added = 0
            for offer in results:
                if not isinstance(offer, dict):
                    continue
                item_id = offer.get("item_id")
                if item_id is not None:
                    if item_id in seen_items:
                        continue
                    seen_items.add(item_id)
                offers.append(offer)
                added += 1
            paging_total = _dict(payload.get("paging")).get("total")
            total = paging_total if isinstance(paging_total, int) else None
            offset += len(results)
            # Stop on: empty page, no paging info (single page), or a page with nothing new.
            if not results or total is None or added == 0:
                break
    except RequestCapReached:
        return OffersResult(offers[:max_offers], captured_at, cap_reached=True)
    # "No offers" only if the API answered (captured_at set); a failed first page is an error.
    no_offers = captured_at is not None and not offers
    return OffersResult(offers[:max_offers], captured_at, no_offers=no_offers)


def _collect_product(
    client: MercadoLibreClient, out: Collected, product_id: str, max_offers: int
) -> None:
    product_path = f"/products/{product_id}"
    product = client.get(product_path)
    if isinstance(product, dict):
        out.add("products", [map_product(product_id, product)], _now())
    elif product is not None:
        client.record_error(product_path, None)  # 200 with a non-object body

    result = fetch_offers(client, product_id, max_offers)
    if result.no_offers:
        out.products_without_offers += 1
    if result.captured_at is not None:
        out.add("offers", (map_offer(product_id, o) for o in result.offers), result.captured_at)
    if result.cap_reached:
        raise RequestCapReached("request cap reached while paginating offers")


def collect(
    client: MercadoLibreClient,
    categories: Iterable[str],
    run_id: str,
    snapshot_date: str,
    max_offers_per_product: int = DEFAULT_MAX_OFFERS_PER_PRODUCT,
) -> Collected:
    """Collect everything. Unexpected exceptions are contained per unit of work (trends, one
    category's highlights, one product) and recorded as ``{path, status: null}``."""
    out = Collected(run_id=run_id, snapshot_date=snapshot_date)
    seen_products: set[str] = set()

    def contained(path: str, work: Callable[[], None]) -> None:
        try:
            work()
        except RequestCapReached:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad unit must not kill the run
            logger.warning("unexpected %s while handling %s; continuing", type(exc).__name__, path)
            client.record_error(path, None)

    def do_trends() -> None:
        path = f"/trends/{SITE_ID}"
        payload = client.get(path)
        if isinstance(payload, list):
            out.add("trends", map_trends(payload), _now())
        elif payload is not None:
            client.record_error(path, None)

    def do_category(category_id: str) -> None:
        path = f"/highlights/{SITE_ID}/category/{category_id}"
        payload = client.get(path)
        if payload is None:
            return
        if not isinstance(payload, dict):
            client.record_error(path, None)
            return
        result = map_highlights(category_id, payload)
        out.skipped_highlights.update(result.skipped)
        if not result.rows:
            # An empty ranking is not a captured category (empty data never counts as success).
            logger.warning("category %s: highlights returned no entries", category_id)
            client.record_error(path, None)
            return
        out.add("highlights", result.rows, _now())
        out.categories_captured.append(category_id)
        followed = [r["product_id"] for r in result.rows if r["type"] == FOLLOWED_TYPE]
        logger.info(
            "category %s: %d ranked entries, %d products followed",
            category_id,
            len(result.rows),
            len(followed),
        )
        for product_id in followed:
            if product_id in seen_products:
                continue
            seen_products.add(product_id)
            contained(
                f"/products/{product_id}",
                lambda pid=product_id: _collect_product(client, out, pid, max_offers_per_product),
            )

    try:
        contained(f"/trends/{SITE_ID}", do_trends)
        for category_id in categories:
            contained(
                f"/highlights/{SITE_ID}/category/{category_id}",
                lambda cid=category_id: do_category(cid),
            )
    except RequestCapReached:
        out.cap_reached = True
        logger.warning("request cap reached; writing what was collected so far")
    return out


def decide_status(collected: Collected, errors: list[dict[str, Any]]) -> str:
    if not collected.categories_captured:
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


def _existing_rank(day_dir: Path) -> int:
    """Rank of the snapshot already on disk; -1 if absent or unreadable."""
    try:
        meta = json.loads((day_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return -1
    return STATUS_RANK.get(meta.get("status") if isinstance(meta, dict) else None, -1)


def ensure_storage_gitignore(out_root: Path) -> None:
    """Make sure temp/backup folders can never be committed to the data repo."""
    out_root.mkdir(parents=True, exist_ok=True)
    path = out_root / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    missing = [p for p in STORAGE_GITIGNORE_PATTERNS if p not in existing]
    if not missing:
        return
    header = [] if existing else ["# Snapshot job temp/backup folders (never commit)"]
    lines = existing + header
    path.write_text("\n".join(lines + missing) + "\n", encoding="utf-8")


def cleanup_stale(out_root: Path) -> None:
    """Remove leftovers of killed runs. A ``.old-<date>-<run>`` whose day folder is missing is the
    only good copy (killed between the two renames), so it is restored instead of deleted."""
    base = out_root / "mercadolibre"
    if not base.is_dir():
        return
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".tmp-"):
            logger.warning("removing stale temp folder %s", entry.name)
            shutil.rmtree(entry, ignore_errors=True)
        elif entry.name.startswith(".old-"):
            day_dir = base / f"snapshot_date={entry.name[len('.old-') :][:10]}"
            if day_dir.exists():
                logger.warning("removing stale backup folder %s", entry.name)
                shutil.rmtree(entry, ignore_errors=True)
            else:
                logger.warning("restoring %s from stale backup %s", day_dir.name, entry.name)
                entry.rename(day_dir)


def write_snapshot(
    out_root: Path, snapshot_date: str, run_meta: dict[str, Any], collected: Collected
) -> Path | None:
    """Write the day folder via temp folder + rename. Returns it, or None if left untouched.

    Replacement rule: the new run replaces the existing folder only if its status ranks at least
    as high (success > partial > failed).
    """
    base = out_root / "mercadolibre"
    day_dir = base / f"snapshot_date={snapshot_date}"
    existing = _existing_rank(day_dir)
    if STATUS_RANK[run_meta["status"]] < existing:
        logger.warning(
            "run is %s; keeping the better existing snapshot for %s",
            run_meta["status"],
            snapshot_date,
        )
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
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)  # never the good copy
        raise

    # os.replace cannot replace a non-empty directory, so: move the old folder aside, move the new
    # one in, and only then delete the old one. If killed between the renames, cleanup_stale()
    # restores the backup on the next run.
    if day_dir.exists():
        day_dir.rename(old_dir)
    try:
        tmp_dir.rename(day_dir)
    except OSError:
        if old_dir.exists():
            try:
                old_dir.rename(day_dir)
            except OSError:
                logger.critical("swap failed; previous snapshot kept at %s", old_dir.name)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
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
    sleep_kwargs: dict[str, Any] = {"sleep": sleep} if sleep is not None else {}

    try:
        tokens = refresh_access_token(
            client_id, client_secret, refresh_token, http=http, **sleep_kwargs
        )
    except ReauthorizationRequired as exc:
        logger.error("%s", exc)
        return EXIT_REAUTHORIZE
    except TransientAuthError as exc:
        logger.error("token refresh failed transiently: %s", exc)
        return EXIT_TOKEN_TRANSIENT
    except AuthError as exc:
        logger.error("token refresh rejected: %s; check the app credentials or re-authorize", exc)
        return EXIT_REAUTHORIZE

    # Persist the rotated token before anything else can fail (ADR-004).
    try:
        persist_refresh_token(new_refresh_token_file, tokens.refresh_token)
    except TokenPersistError as exc:
        logger.error("%s; the owner will need to re-authorize", exc)
        return EXIT_TOKEN_PERSIST
    logger.info("new refresh token persisted")

    step = "ensure_storage_gitignore"
    try:
        ensure_storage_gitignore(out_root)
        step = "cleanup_stale"
        cleanup_stale(out_root)
        step = "collect"
        with MercadoLibreClient(
            tokens.access_token,
            base_url=base_url,
            delay_s=delay_s,
            max_requests=max_requests,
            http=http,
            **sleep_kwargs,
        ) as client:
            collected = collect(client, categories, run_id, snapshot_date, max_offers_per_product)
            errors = client.error_summary()
            requests_made = client.requests_made

        step = "build_run_meta"
        status = decide_status(collected, errors)
        run_meta = {
            "run_id": run_id,
            "snapshot_date": snapshot_date,
            "started_at": started_at,
            "finished_at": _now(),
            "site_id": SITE_ID,
            "categories": collected.categories_captured,
            "categories_requested": list(categories),
            "requests_made": requests_made,
            "request_cap": max_requests,
            "request_cap_reached": collected.cap_reached,
            "row_counts": {kind: len(collected.rows[kind]) for kind in FILES},
            "skipped_highlights": dict(sorted(collected.skipped_highlights.items())),
            "products_without_offers": collected.products_without_offers,
            "errors": errors,
            "status": status,
        }
        step = "write_snapshot"
        write_snapshot(out_root, snapshot_date, run_meta, collected)
    except Exception as exc:  # noqa: BLE001 - token is already safe; report and fail the run
        # Step name and exception type only, never values.
        logger.error("snapshot aborted in step %s by unexpected %s", step, type(exc).__name__)
        return EXIT_FAILED

    logger.info(
        "snapshot %s status=%s requests=%d rows=%s skipped=%s errors=%d",
        snapshot_date,
        status,
        requests_made,
        run_meta["row_counts"],
        run_meta["skipped_highlights"],
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
        return EXIT_CONFIG
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
