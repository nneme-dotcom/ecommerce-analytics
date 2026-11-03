"""Synthetic API payloads shaped like docs/API_PROBE.md.

Only fields listed in the probe are present. Values are made up (except the probe's own example
product MLA58353028). No real tokens and no recorded third-party data.
"""

from __future__ import annotations

import copy


def highlights(entries: list[str | tuple[str, str]]) -> dict:
    """``/highlights/MLA/category/{cat}``: ``content[]`` of ``{id, position, type}``.

    ``type`` is PRODUCT, USER_PRODUCT or ITEM (verified live 2026-11-03). A bare string entry is a
    PRODUCT; pass ``(id, type)`` for the other types.
    """
    content = []
    for position, entry in enumerate(entries, start=1):
        entry_id, entry_type = (entry, "PRODUCT") if isinstance(entry, str) else entry
        content.append({"id": entry_id, "position": position, "type": entry_type})
    return {"content": content}


def product(
    product_id: str, brand: str | None = "Motorola", model: str | None = "Moto G06"
) -> dict:
    """``/products/{id}``: name, domain_id, attributes, pictures, buy_box_winner (may be null)."""
    attributes = []
    if brand is not None:
        attributes.append({"id": "BRAND", "name": "Marca", "value_name": brand})
    if model is not None:
        attributes.append({"id": "MODEL", "name": "Modelo", "value_name": model})
    attributes.append({"id": "COLOR", "name": "Color", "value_name": "Azul"})
    return {
        "id": product_id,
        "name": f"Product {product_id}",
        "domain_id": "MLA-CELLPHONES",
        "attributes": attributes,
        "pictures": [{"id": "pic-1", "url": "https://example.invalid/pic-1.jpg"}],
        "buy_box_winner": None,
    }


OFFER_TEMPLATE = {
    "item_id": "MLA1000000001",
    "site_id": "MLA",
    "seller_id": 111,
    "accepts_mercadopago": True,
    "price": 255999.36,
    "category_id": "MLA1055",
    "currency_id": "ARS",
    "warranty": "12 months",
    "condition": "new",
    "listing_type_id": "gold_pro",
    "international_delivery_mode": "none",
    "tier": "none",
    "inventory_id": None,
    "tags": ["good_quality_thumbnail"],
    "deal_ids": ["DEAL1"],
    "official_store_id": None,
    "original_price": 399999,
    "shipping": {
        "cost": 0,
        "free_shipping": True,
        "local_pick_up": False,
        "logistic_type": "cross_docking",
        "mode": "me2",
        "store_pick_up": False,
        "tags": [],
    },
    "seller_address": {"state": {"name": "Capital Federal"}},
    "sale_terms": [],
    "user_product_id": "MLAU1",
    "min_purchase_unit": 1,
}


def offer(n: int, **overrides) -> dict:
    data = copy.deepcopy(OFFER_TEMPLATE)
    data["item_id"] = f"MLA{1000000000 + n}"
    data["seller_id"] = 100 + n
    data.update(overrides)
    return data


def offers_page(offers: list[dict], offset: int, limit: int, total: int) -> dict:
    """``/products/{id}/items``: ``results[]`` plus ``paging``."""
    return {"paging": {"total": total, "offset": offset, "limit": limit}, "results": offers}


def trends(keywords: list[str]) -> list[dict]:
    """``/trends/MLA``: list of trending keywords."""
    return [
        {"keyword": k, "url": f"https://listado.mercadolibre.com.ar/{k.replace(' ', '-')}"}
        for k in keywords
    ]
