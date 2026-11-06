"""Synthetic Mercado Libre day folders for the CI ``dbt build`` (task 2.2, ADR-016).

The folders are written by the **real snapshot writer** (``ingestion.mercadolibre.snapshot``:
``map_highlights`` / ``map_product`` / ``map_offer`` / ``map_trends`` + ``write_snapshot``) from the
synthetic API payloads of ``tests/mercadolibre/ml_fixtures.py`` (shaped like docs/API_PROBE.md),
so ``fixture_db`` can load them with the real raw loader ``ingestion.mercadolibre.load``. All ids,
names and prices are made up; no real Mercado Libre data is used (ADR-003). Run ids and
timestamps are fixed, so the files are byte-identical on every run.

Days and the cases they exercise (DATA_CONTRACT §3, ADR-019; the full list is in
warehouse/fixtures/README.md):

- 2026-11-03 ``success``: P1 ranked 1st in two categories (tie: MLA1055 by byte order); a
  USER_PRODUCT and an ITEM highlight (never followed); P3 has no offers; offers whose
  original_price is > price (discount), = price, NULL and < price; an official-store
  fulfillment offer; a listing-only category (MLA3502).
- 2026-11-04 ``partial``: category MLA1652 failed; the P2 and P5 detail calls failed (their
  offers are kept; P5 never has details); offers O1, O2, O4, O5 again (two days of one offer:
  a price drop, original < price); sellers 101 (two states on one day) and 102 (moved); four
  malformed offers of P1 (O8 without item_id, O9 without seller_id, O10 without price, O11
  without currency_id): kept in staging, left out of the fact, and the reason the fixture
  ``dbt build`` reports exactly WARN=4 (the four staging not_null warnings, ADR-019).
- 2026-11-05 ``failed``: no category captured, trends only: left out of core, still a day of
  dim_date.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

from ingestion.mercadolibre import snapshot

REPO_ROOT = Path(__file__).resolve().parents[2]
ML_FIXTURES = REPO_ROOT / "tests" / "mercadolibre" / "ml_fixtures.py"

P1, P2, P3, P4, P5 = (f"MLA9000000{n}" for n in range(1, 6))
SUCCESS_DAY, PARTIAL_DAY, FAILED_DAY = "2026-11-03", "2026-11-04", "2026-11-05"
DAYS = (SUCCESS_DAY, PARTIAL_DAY, FAILED_DAY)
EXPECTED_STATUS = {SUCCESS_DAY: "success", PARTIAL_DAY: "partial", FAILED_DAY: "failed"}


def _payloads() -> ModuleType:
    """``tests/mercadolibre/ml_fixtures.py`` by path (``tests/`` is not a package)."""
    spec = importlib.util.spec_from_file_location("warehouse_ci_ml_fixtures", ML_FIXTURES)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {ML_FIXTURES}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _offer(fx: ModuleType, n: int, **overrides: Any) -> dict[str, Any]:
    """Offer ``O<n>`` (item MLA100000000<n>, seller 100+n unless overridden)."""
    return fx.offer(n, **overrides)


def _day_content(fx: ModuleType, day: str) -> dict[str, Any]:
    """Everything one day's run collected: highlights per category, products, offers, errors."""
    seller_ba = {"seller_address": {"state": {"name": "Buenos Aires"}}}
    store = {
        "official_store_id": 77,
        "shipping": {"free_shipping": True, "logistic_type": "fulfillment", "mode": "me2"},
    }
    if day == SUCCESS_DAY:
        return {
            "highlights": {
                "MLA1055": [P1, P2, P3, ("MLAU900", "USER_PRODUCT")],
                "MLA1652": [P1, P4, ("MLA900000099", "ITEM")],
            },
            "products": {
                P1: fx.product(P1),
                P2: fx.product(P2, brand="Samsung", model="A15"),
                P3: fx.product(P3, brand=None),
                P4: fx.product(P4, model=None),
            },
            "offers": {
                P1: [
                    _offer(fx, 1, price=100000, original_price=125000),  # 20 % discount
                    _offer(fx, 2, price=98000.5, original_price=None),  # no "before" price
                    _offer(fx, 3, price=101000, original_price=101000),  # equal: no discount
                ],
                P2: [
                    _offer(
                        fx,
                        4,
                        price=50000,
                        original_price=40000,
                        category_id="MLA3502",
                        seller_id=101,
                    ),  # original < price; seller 101 has two offers
                    _offer(
                        fx,
                        5,
                        price=52000,
                        original_price=None,
                        category_id="MLA3502",
                        deal_ids=[],
                        **store,
                    ),
                ],
                P3: [],  # products_without_offers
                P4: [
                    _offer(
                        fx,
                        6,
                        price=900000,
                        original_price=1000000,
                        category_id="MLA1652",
                        seller_address={"state": {"name": "Córdoba"}},
                    )
                ],
            },
            "categories_requested": ["MLA1055", "MLA1652"],
            "errors": [],
            "trends": ["celular", "notebook"],
        }
    if day == PARTIAL_DAY:
        renamed = fx.product(P1)
        renamed["name"] = f"Product {P1} (renamed)"
        return {
            "highlights": {"MLA1055": [P2, P1, P5, P3]},  # MLA1652 failed
            "products": {P1: renamed, P3: fx.product(P3, brand=None)},  # P2, P5 details failed
            "offers": {
                P2: [
                    _offer(
                        fx,
                        4,
                        price=50000,
                        original_price=40000,
                        category_id="MLA3502",
                        seller_id=101,
                    ),
                    _offer(
                        fx,
                        5,
                        price=51000,
                        original_price=None,
                        category_id="MLA3502",
                        deal_ids=[],
                        **store,
                    ),
                ],
                P1: [
                    # Seller 101 now gives two states on one day (O4 keeps Capital Federal):
                    # the lowest in byte order wins. Seller 102 moved to Mendoza (latest wins).
                    _offer(fx, 1, price=95000, original_price=125000, **seller_ba),  # dropped
                    _offer(
                        fx,
                        2,
                        price=99000,
                        original_price=98000,  # original < price
                        seller_address={"state": {"name": "Mendoza"}},
                    ),
                    # Malformed offers (one missing column each): staging warns, the fact leaves
                    # them out. Seller 101 (no state in the template), so no dimension changes.
                    _offer(fx, 8, item_id=None, seller_id=101),
                    _offer(fx, 9, seller_id=None),
                    _offer(fx, 10, price=None, seller_id=101),
                    _offer(fx, 11, currency_id=None, seller_id=101),
                ],
                P5: [_offer(fx, 7, price=75000, original_price=None)],
                P3: [],
            },
            "categories_requested": ["MLA1055", "MLA1652"],
            "errors": [
                {"path": "/highlights/MLA/category/MLA1652", "status": 500, "count": 4},
                {"path": f"/products/{P2}", "status": 500, "count": 4},
                {"path": f"/products/{P5}", "status": None, "count": 1},
            ],
            "trends": ["celular", "zapatillas"],
        }
    if day == FAILED_DAY:
        return {
            "highlights": {},
            "products": {},
            "offers": {},
            "categories_requested": ["MLA1055", "MLA1652"],
            "errors": [
                {"path": "/highlights/MLA/category/MLA1055", "status": 500, "count": 4},
                {"path": "/highlights/MLA/category/MLA1652", "status": 500, "count": 4},
            ],
            "trends": ["aire acondicionado", "celular"],
        }
    raise ValueError(f"no fixture day {day}")


def write_day(fx: ModuleType, root: Path, day: str) -> Path:
    """Map one day's payloads with the snapshot mappers and write its folder; return it."""
    content = _day_content(fx, day)
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ecommerce-analytics/warehouse-fixture/{day}"))
    collected = snapshot.Collected(run_id=run_id, snapshot_date=day)
    ts = f"{day}T06:00:12Z"
    collected.add("trends", snapshot.map_trends(fx.trends(content["trends"])), ts)
    for category_id, entries in content["highlights"].items():
        result = snapshot.map_highlights(category_id, fx.highlights(entries))
        collected.add("highlights", result.rows, ts)
        collected.categories_captured.append(category_id)
        collected.skipped_highlights.update(result.skipped)
    for product_id, payload in content["products"].items():
        collected.add("products", [snapshot.map_product(product_id, payload)], ts)
    for product_id, offers in content["offers"].items():
        if not offers:
            collected.products_without_offers += 1
            continue
        collected.add("offers", [snapshot.map_offer(product_id, o) for o in offers], ts)
    errors = content["errors"]
    status = snapshot.decide_status(collected, errors)
    if status != EXPECTED_STATUS[day]:
        raise RuntimeError(f"fixture day {day} would be {status}, not {EXPECTED_STATUS[day]}")
    meta = {
        "run_id": run_id,
        "snapshot_date": day,
        "started_at": f"{day}T06:00:00Z",
        "finished_at": f"{day}T06:03:00Z",
        "site_id": snapshot.SITE_ID,
        "categories": collected.categories_captured,
        "categories_requested": content["categories_requested"],
        "requests_made": 20,
        "request_cap": snapshot.DEFAULT_MAX_REQUESTS,
        "request_cap_reached": False,
        "row_counts": {kind: len(collected.rows[kind]) for kind in snapshot.FILES},
        "skipped_highlights": dict(sorted(collected.skipped_highlights.items())),
        "products_without_offers": collected.products_without_offers,
        "errors": errors,
        "status": status,
    }
    day_dir = snapshot.write_snapshot(root, day, meta, collected)
    if day_dir is None:  # only if a better day folder already exists under root
        raise RuntimeError(f"fixture day {day} was not written")
    return day_dir


def write_days(root: Path) -> list[Path]:
    """Write every fixture day under ``root/mercadolibre/`` (``root`` = a data-repo-like root)."""
    fx = _payloads()
    return [write_day(fx, root, day) for day in DAYS]
