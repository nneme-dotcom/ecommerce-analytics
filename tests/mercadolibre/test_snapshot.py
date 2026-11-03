import gzip
import json
import logging
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import ml_fixtures as fx
import pytest

from ingestion.mercadolibre import snapshot

DATE = "2026-11-03"
OLD_REFRESH = "fake-refresh-old"
NEW_REFRESH = "fake-refresh-new"
ACCESS = "fake-access-xyz"
SECRET = "fake-client-secret"

CONTRACT_FIELDS = {
    "highlights": {"category_id", "position", "product_id", "type"},
    "products": {
        "product_id",
        "name",
        "domain_id",
        "family_name",
        "status",
        "brand",
        "model",
        "permalink",
    },
    "offers": {
        "product_id",
        "item_id",
        "seller_id",
        "official_store_id",
        "price",
        "original_price",
        "currency_id",
        "condition",
        "listing_type_id",
        "category_id",
        "free_shipping",
        "logistic_type",
        "shipping_mode",
        "deal_ids",
        "tags",
        "seller_state",
    },
    "trends": {"position", "keyword", "url"},
}
META_FIELDS = {"snapshot_date", "run_id", "captured_at"}
RUN_FIELDS = {
    "run_id",
    "snapshot_date",
    "started_at",
    "finished_at",
    "site_id",
    "categories",
    "categories_requested",
    "requests_made",
    "request_cap",
    "request_cap_reached",
    "row_counts",
    "skipped_highlights",
    "products_without_offers",
    "errors",
    "status",
}


class FakeML:
    """Routes mocked requests: token endpoint + API paths. Override routes per test.

    ``/products/{id}/items`` behaves as verified live: paging {limit, offset, total}, and an offset
    at or beyond total (offset > 0) returns 404.
    """

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.token_responses: list[httpx.Response] = []
        many = [fx.offer(i) for i in range(120)]
        self.offers = {"MLA58353028": many, "MLA2": [fx.offer(500)], "MLA3": [fx.offer(600)]}
        self.routes = {
            "/trends/MLA": lambda r: httpx.Response(
                200, json=fx.trends(["iphone", "aire acondicionado"])
            ),
            "/highlights/MLA/category/MLA1055": lambda r: httpx.Response(
                200,
                json=fx.highlights(["MLA58353028", "MLA2", ("MLAU77", "USER_PRODUCT")]),
            ),
            "/highlights/MLA/category/MLA1652": lambda r: httpx.Response(
                200,  # MLA2 ranks in both categories
                json=fx.highlights(["MLA3", ("MLA900", "ITEM"), "MLA2"]),
            ),
            "/products/MLA58353028": lambda r: httpx.Response(200, json=fx.product("MLA58353028")),
            "/products/MLA2": lambda r: httpx.Response(200, json=fx.product("MLA2", brand=None)),
            "/products/MLA3": lambda r: httpx.Response(200, json=fx.product("MLA3")),
        }

    def _items(self, request, product_id):
        q = parse_qs(request.url.query.decode())
        offset, limit = int(q["offset"][0]), int(q["limit"][0])
        all_offers = self.offers[product_id]
        if offset > 0 and offset >= len(all_offers):
            return httpx.Response(404, json={"message": "not found"})
        page = all_offers[offset : offset + limit]
        return httpx.Response(200, json=fx.offers_page(page, offset, limit, len(all_offers)))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path == "/oauth/token":
            if self.token_responses:
                return self.token_responses.pop(0)
            return httpx.Response(
                200,
                json={"access_token": ACCESS, "refresh_token": NEW_REFRESH, "expires_in": 21600},
            )
        assert request.headers["authorization"] == f"Bearer {ACCESS}"
        assert ACCESS not in str(request.url)
        if path in self.routes:
            return self.routes[path](request)
        if path.startswith("/products/") and path.endswith("/items"):
            product_id = path.split("/")[2]
            if f"items:{product_id}" in self.routes:
                return self.routes[f"items:{product_id}"](request)
            return self._items(request, product_id)
        return httpx.Response(404, json={"message": "not found"})

    def paths(self):
        return [c.url.path for c in self.calls]

    def item_pages(self, product_id):
        return [
            (int(q["offset"][0]), int(q["limit"][0]))
            for q in (
                parse_qs(c.url.query.decode())
                for c in self.calls
                if c.url.path == f"/products/{product_id}/items"
            )
        ]


@pytest.fixture
def fake():
    return FakeML()


def do_run(fake, tmp_path, **kwargs):
    params = {
        "out_root": tmp_path / "store",
        "snapshot_date": DATE,
        "categories": ["MLA1055", "MLA1652"],
        "new_refresh_token_file": tmp_path / "secrets" / "new_refresh_token",
        "client_id": "app-id",
        "client_secret": SECRET,
        "refresh_token": OLD_REFRESH,
        "delay_s": 0,
        "sleep": lambda s: None,
        "http": httpx.Client(transport=httpx.MockTransport(fake)),
    }
    params.update(kwargs)
    return snapshot.run(**params)


def base_dir(tmp_path):
    return tmp_path / "store" / "mercadolibre"


def day_dir(tmp_path):
    return base_dir(tmp_path) / f"snapshot_date={DATE}"


def read_jsonl(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def read_run(tmp_path):
    return json.loads((day_dir(tmp_path) / "run.json").read_text(encoding="utf-8"))


def rows(tmp_path, kind):
    return read_jsonl(day_dir(tmp_path) / f"{kind}.jsonl.gz")


def token_file(tmp_path):
    return tmp_path / "secrets" / "new_refresh_token"


# --------------------------------------------------------------------------- happy path


def test_happy_path_writes_contract_layout(fake, tmp_path):
    assert do_run(fake, tmp_path) == snapshot.EXIT_OK

    folder = day_dir(tmp_path)
    assert sorted(p.name for p in folder.iterdir()) == [
        "highlights.jsonl.gz",
        "offers.jsonl.gz",
        "products.jsonl.gz",
        "run.json",
        "trends.jsonl.gz",
    ]
    run = read_run(tmp_path)
    assert set(run) == RUN_FIELDS
    assert run["status"] == "success"
    assert run["site_id"] == "MLA"
    assert run["snapshot_date"] == DATE
    assert run["categories"] == ["MLA1055", "MLA1652"]
    assert run["categories_requested"] == ["MLA1055", "MLA1652"]
    assert run["errors"] == []
    assert run["request_cap_reached"] is False
    assert run["skipped_highlights"] == {"ITEM": 1, "USER_PRODUCT": 1}
    assert run["products_without_offers"] == 0

    data = {k: rows(tmp_path, k) for k in CONTRACT_FIELDS}
    for kind, records in data.items():
        assert records, kind
        for record in records:
            assert set(record) == CONTRACT_FIELDS[kind] | META_FIELDS, kind
            assert record["snapshot_date"] == DATE
            assert record["run_id"] == run["run_id"]
            assert record["captured_at"].endswith("Z")
    assert run["row_counts"] == {k: len(v) for k, v in data.items()}

    # ALL highlight lines are written (positions stay complete); only PRODUCT is followed
    assert [
        (h["category_id"], h["position"], h["product_id"], h["type"]) for h in data["highlights"]
    ] == [
        ("MLA1055", 1, "MLA58353028", "PRODUCT"),
        ("MLA1055", 2, "MLA2", "PRODUCT"),
        ("MLA1055", 3, "MLAU77", "USER_PRODUCT"),
        ("MLA1652", 1, "MLA3", "PRODUCT"),
        ("MLA1652", 2, "MLA900", "ITEM"),
        ("MLA1652", 3, "MLA2", "PRODUCT"),
    ]
    assert "/products/MLAU77" not in fake.paths()
    assert "/products/MLA900/items" not in fake.paths()
    # products are deduplicated across categories and fetched once
    assert [p["product_id"] for p in data["products"]] == ["MLA58353028", "MLA2", "MLA3"]
    assert fake.paths().count("/products/MLA2") == 1
    assert [(t["position"], t["keyword"]) for t in data["trends"]] == [
        (1, "iphone"),
        (2, "aire acondicionado"),
    ]


def test_record_mapping_matches_probe_fields(fake, tmp_path):
    fake.offers["MLA3"] = [fx.offer(1, seller_address=None, official_store_id=55, deal_ids=None)]
    do_run(fake, tmp_path)
    products = {p["product_id"]: p for p in rows(tmp_path, "products")}
    assert products["MLA58353028"]["brand"] == "Motorola"
    assert products["MLA58353028"]["model"] == "Moto G06"
    assert products["MLA2"]["brand"] is None
    # not confirmed by the probe -> nullable
    assert products["MLA3"]["family_name"] is None
    assert products["MLA3"]["permalink"] is None

    offers = rows(tmp_path, "offers")
    first = next(o for o in offers if o["product_id"] == "MLA58353028")
    assert first["price"] == 255999.36
    assert first["original_price"] == 399999
    assert first["free_shipping"] is True
    assert first["logistic_type"] == "cross_docking"
    assert first["shipping_mode"] == "me2"
    assert first["seller_state"] == "Capital Federal"
    assert first["deal_ids"] == ["DEAL1"]
    odd = next(o for o in offers if o["product_id"] == "MLA3")
    assert odd["seller_state"] is None
    assert odd["official_store_id"] == 55
    assert odd["deal_ids"] == []


# --------------------------------------------------------------------------- pagination


def test_offers_are_paginated_and_capped(fake, tmp_path):
    do_run(fake, tmp_path, max_offers_per_product=100)
    top = [o for o in rows(tmp_path, "offers") if o["product_id"] == "MLA58353028"]
    assert len(top) == 100  # 120 available, capped at 100
    assert len({o["item_id"] for o in top}) == 100
    assert fake.item_pages("MLA58353028") == [(0, 50), (50, 50)]


def test_pagination_reads_all_pages_and_never_requests_offset_at_total(fake, tmp_path):
    do_run(fake, tmp_path, max_offers_per_product=500)
    top = [o for o in rows(tmp_path, "offers") if o["product_id"] == "MLA58353028"]
    assert len(top) == 120
    assert fake.item_pages("MLA58353028") == [(0, 50), (50, 50), (100, 50)]
    assert all(offset < 120 for offset, _ in fake.item_pages("MLA58353028"))
    # single-page products: one request only, no follow-up at offset == total
    assert fake.item_pages("MLA2") == [(0, 50)]
    assert read_run(tmp_path)["errors"] == []


def test_total_zero_gives_no_offers_and_no_further_pages(fake, tmp_path):
    fake.offers["MLA2"] = []  # 200 with paging.total == 0 and results []
    do_run(fake, tmp_path)
    assert "MLA2" not in {o["product_id"] for o in rows(tmp_path, "offers")}
    assert fake.item_pages("MLA2") == [(0, 50)]
    run = read_run(tmp_path)
    assert run["status"] == "success"
    assert run["errors"] == []
    assert run["products_without_offers"] == 1


def test_missing_paging_is_treated_as_single_page(fake, tmp_path):
    fake.routes["items:MLA2"] = lambda r: httpx.Response(
        200, json={"results": [fx.offer(1), fx.offer(2)]}
    )
    do_run(fake, tmp_path)
    assert len([o for o in rows(tmp_path, "offers") if o["product_id"] == "MLA2"]) == 2
    assert fake.item_pages("MLA2") == [(0, 50)]


def test_404_on_later_page_is_end_of_data_not_an_error(fake, tmp_path):
    def lying_total(request):
        q = parse_qs(request.url.query.decode())
        offset = int(q["offset"][0])
        if offset > 0:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json=fx.offers_page([fx.offer(1)], 0, 50, total=60))

    fake.routes["items:MLA2"] = lying_total
    do_run(fake, tmp_path)
    run = read_run(tmp_path)
    assert run["errors"] == []
    assert run["status"] == "success"
    assert len([o for o in rows(tmp_path, "offers") if o["product_id"] == "MLA2"]) == 1


def test_404_on_first_page_means_no_offers_not_an_error(fake, tmp_path):
    # e.g. an out-of-stock best seller: must not turn every run into `partial`
    fake.routes["items:MLA2"] = lambda r: httpx.Response(404, json={})
    assert do_run(fake, tmp_path) == snapshot.EXIT_OK
    run = read_run(tmp_path)
    assert run["status"] == "success"
    assert run["errors"] == []
    assert run["products_without_offers"] == 1
    assert fake.item_pages("MLA2") == [(0, 50)]
    assert "MLA2" not in {o["product_id"] for o in rows(tmp_path, "offers")}


def test_failed_first_page_is_an_error_not_no_offers(fake, tmp_path):
    fake.routes["items:MLA2"] = lambda r: httpx.Response(500, json={})
    do_run(fake, tmp_path)
    run = read_run(tmp_path)
    assert run["products_without_offers"] == 0
    assert {"path": "/products/MLA2/items", "status": 500, "count": 1} in run["errors"]


def test_pagination_stops_when_a_page_adds_no_new_item(fake, tmp_path):
    same_page = [fx.offer(i) for i in range(50)]

    def repeating(request):
        offset = int(parse_qs(request.url.query.decode())["offset"][0])
        return httpx.Response(200, json=fx.offers_page(same_page, offset, 50, total=500))

    fake.routes["items:MLA2"] = repeating
    do_run(fake, tmp_path, max_offers_per_product=500)
    assert fake.item_pages("MLA2") == [(0, 50), (50, 50)]
    mla2 = [o for o in rows(tmp_path, "offers") if o["product_id"] == "MLA2"]
    assert len(mla2) == 50


def test_offers_deduplicated_by_item_id_across_shifting_pages(fake, tmp_path):
    # Listing order shifts between pages: page 2 repeats two offers from page 1.
    page1 = [fx.offer(i) for i in range(50)]
    page2 = [fx.offer(48), fx.offer(49)] + [fx.offer(i) for i in range(50, 55)]

    def shifting(request):
        offset = int(parse_qs(request.url.query.decode())["offset"][0])
        page = page1 if offset == 0 else page2
        return httpx.Response(200, json=fx.offers_page(page, offset, 50, total=57))

    fake.routes["items:MLA2"] = shifting
    do_run(fake, tmp_path)
    mla2 = [o["item_id"] for o in rows(tmp_path, "offers") if o["product_id"] == "MLA2"]
    assert len(mla2) == len(set(mla2)) == 55


def test_highlights_deduplicated_per_category_and_product(fake, tmp_path):
    fake.routes["/highlights/MLA/category/MLA1055"] = lambda r: httpx.Response(
        200, json={"content": [{"id": "MLA2", "position": 1, "type": "PRODUCT"}] * 2}
    )
    do_run(fake, tmp_path)
    mla1055 = [h for h in rows(tmp_path, "highlights") if h["category_id"] == "MLA1055"]
    assert [(h["position"], h["product_id"]) for h in mla1055] == [(1, "MLA2")]


# --------------------------------------------------------------------------- malformed shapes


@pytest.mark.parametrize(
    "bad_offer",
    [
        {"item_id": "MLA1", "shipping": "x", "seller_address": "y", "deal_ids": "z", "tags": 5},
        {"item_id": "MLA1", "seller_address": {"state": "Buenos Aires"}},
        {"item_id": "MLA1", "shipping": None, "seller_address": {"state": None}},
    ],
)
def test_map_offer_tolerates_malformed_shapes(bad_offer):
    row = snapshot.map_offer("P", bad_offer)
    assert set(row) == CONTRACT_FIELDS["offers"]
    assert row["free_shipping"] is None
    assert row["seller_state"] is None
    assert row["deal_ids"] == [] and row["tags"] == []


def test_map_product_tolerates_malformed_attributes():
    row = snapshot.map_product("P1", {"attributes": "oops", "name": "x"})
    assert row["product_id"] == "P1" and row["brand"] is None and row["model"] is None
    row = snapshot.map_product("P1", {"attributes": [None, 3, {"id": "BRAND", "value_name": 7}]})
    assert row["brand"] is None


def test_map_highlights_tolerates_malformed_entries():
    result = snapshot.map_highlights(
        "C", {"content": [None, "x", {"position": 1}, {"id": "A", "position": "1", "type": 3}]}
    )
    assert result.rows == [{"category_id": "C", "position": None, "product_id": "A", "type": None}]
    assert result.skipped == {"MALFORMED": 3, "UNKNOWN": 1}
    assert snapshot.map_highlights("C", {"content": "nope"}).rows == []
    assert snapshot.map_highlights("C", ["not", "a", "dict"]).rows == []


def test_malformed_payloads_do_not_kill_the_run(fake, tmp_path):
    fake.routes["/products/MLA2"] = lambda r: httpx.Response(200, json=["not", "an", "object"])
    fake.routes["items:MLA3"] = lambda r: httpx.Response(
        200, json={"paging": "x", "results": [{"item_id": "MLA1", "shipping": "x"}, 7, None]}
    )
    fake.routes["/trends/MLA"] = lambda r: httpx.Response(200, json={"unexpected": True})
    assert do_run(fake, tmp_path) == snapshot.EXIT_OK
    run = read_run(tmp_path)
    assert run["status"] == "partial"
    assert {"path": "/products/MLA2", "status": None, "count": 1} in run["errors"]
    assert {"path": "/trends/MLA", "status": None, "count": 1} in run["errors"]
    mla3 = [o for o in rows(tmp_path, "offers") if o["product_id"] == "MLA3"]
    assert [o["item_id"] for o in mla3] == ["MLA1"]
    assert mla3[0]["free_shipping"] is None


def test_unexpected_exception_is_contained_per_product(fake, tmp_path, monkeypatch):
    real = snapshot.map_offer

    def explode_for_mla2(product_id, offer):
        if product_id == "MLA2":
            raise AttributeError("boom")
        return real(product_id, offer)

    monkeypatch.setattr(snapshot, "map_offer", explode_for_mla2)
    assert do_run(fake, tmp_path) == snapshot.EXIT_OK
    run = read_run(tmp_path)
    assert run["status"] == "partial"
    assert {"path": "/products/MLA2", "status": None, "count": 1} in run["errors"]
    assert {"MLA58353028", "MLA3"} <= {o["product_id"] for o in rows(tmp_path, "offers")}


def test_crash_in_transport_is_contained_and_token_already_persisted(fake, tmp_path):
    def boom(request):
        raise RuntimeError("unexpected crash")

    fake.routes["/trends/MLA"] = boom
    assert do_run(fake, tmp_path) == snapshot.EXIT_OK
    assert token_file(tmp_path).read_text() == NEW_REFRESH
    run = read_run(tmp_path)
    assert run["status"] == "partial"
    assert {"path": "/trends/MLA", "status": None, "count": 1} in run["errors"]


def test_write_failure_returns_failed_but_token_is_persisted(fake, tmp_path, caplog):
    out_root = tmp_path / "store-is-a-file"
    out_root.write_text("x")
    assert do_run(fake, tmp_path, out_root=out_root) == snapshot.EXIT_FAILED
    assert token_file(tmp_path).read_text() == NEW_REFRESH
    assert "aborted in step ensure_storage_gitignore by unexpected FileExistsError" in caplog.text


# --------------------------------------------------------------------------- token handling


def test_rotated_token_written_to_file(fake, tmp_path):
    do_run(fake, tmp_path)
    assert token_file(tmp_path).read_text(encoding="utf-8") == NEW_REFRESH
    form = parse_qs(fake.calls[0].content.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == [OLD_REFRESH]


def test_invalid_grant_exits_2_and_writes_nothing(fake, tmp_path, caplog):
    fake.token_responses = [httpx.Response(400, json={"error": "invalid_grant"})]
    assert do_run(fake, tmp_path) == snapshot.EXIT_REAUTHORIZE == 2
    assert not token_file(tmp_path).exists()
    assert not (tmp_path / "store").exists()
    assert "re-authorize" in caplog.text
    assert fake.paths() == ["/oauth/token"]


def test_transient_token_failure_is_retried_then_succeeds(fake, tmp_path):
    fake.token_responses = [httpx.Response(503), httpx.Response(429)]
    assert do_run(fake, tmp_path) == snapshot.EXIT_OK
    assert fake.paths()[:3] == ["/oauth/token"] * 3
    assert token_file(tmp_path).read_text() == NEW_REFRESH


def test_transient_token_failure_after_retries_exits_4(fake, tmp_path):
    fake.token_responses = [httpx.Response(502)] * 4
    assert do_run(fake, tmp_path) == snapshot.EXIT_TOKEN_TRANSIENT == 4
    assert fake.paths() == ["/oauth/token"] * 4  # 1 try + 3 retries
    assert not token_file(tmp_path).exists()
    assert not (tmp_path / "store").exists()


def test_token_persist_failure_exits_3_before_any_data_request(fake, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    code = do_run(fake, tmp_path, new_refresh_token_file=blocker / "token")
    assert code == snapshot.EXIT_TOKEN_PERSIST
    assert fake.paths() == ["/oauth/token"]


# --------------------------------------------------------------------------- status and replacement


def test_partial_when_some_requests_fail(fake, tmp_path):
    fake.routes["items:MLA2"] = lambda r: httpx.Response(500, json={})
    fake.routes["/products/MLA3"] = lambda r: httpx.Response(404, json={})
    assert do_run(fake, tmp_path) == snapshot.EXIT_OK
    run = read_run(tmp_path)
    assert run["status"] == "partial"
    assert run["errors"] == [
        {"path": "/products/MLA2/items", "status": 500, "count": 1},
        {"path": "/products/MLA3", "status": 404, "count": 1},
    ]
    offer_products = {o["product_id"] for o in rows(tmp_path, "offers")}
    assert "MLA2" not in offer_products
    assert "MLA3" in offer_products  # offers still fetched


def test_categories_lists_only_captured_ones(fake, tmp_path):
    fake.routes["/highlights/MLA/category/MLA1652"] = lambda r: httpx.Response(404, json={})
    do_run(fake, tmp_path)
    run = read_run(tmp_path)
    assert run["categories"] == ["MLA1055"]
    assert run["categories_requested"] == ["MLA1055", "MLA1652"]
    assert run["status"] == "partial"


def test_empty_highlights_category_is_not_captured(fake, tmp_path):
    fake.routes["/highlights/MLA/category/MLA1652"] = lambda r: httpx.Response(
        200, json={"content": []}
    )
    do_run(fake, tmp_path)
    run = read_run(tmp_path)
    assert run["categories"] == ["MLA1055"]
    assert run["status"] == "partial"
    assert {"path": "/highlights/MLA/category/MLA1652", "status": None, "count": 1} in run["errors"]


def _empty_highlights():
    empty = FakeML()
    for cat in ("MLA1055", "MLA1652"):
        empty.routes[f"/highlights/MLA/category/{cat}"] = lambda r: httpx.Response(
            200, json={"content": []}
        )
    return empty


def test_all_empty_highlights_is_failed(tmp_path):
    assert do_run(_empty_highlights(), tmp_path) == snapshot.EXIT_FAILED
    run = read_run(tmp_path)
    assert run["status"] == "failed"
    assert run["categories"] == []
    assert run["row_counts"]["highlights"] == 0


def test_empty_highlights_rerun_does_not_replace_success(fake, tmp_path):
    assert do_run(fake, tmp_path) == snapshot.EXIT_OK
    good = read_run(tmp_path)
    assert good["status"] == "success"
    assert do_run(_empty_highlights(), tmp_path) == snapshot.EXIT_FAILED
    assert read_run(tmp_path)["run_id"] == good["run_id"]
    assert rows(tmp_path, "highlights")


def test_failed_when_no_highlights(fake, tmp_path):
    for cat in ("MLA1055", "MLA1652"):
        fake.routes[f"/highlights/MLA/category/{cat}"] = lambda r: httpx.Response(404, json={})
    assert do_run(fake, tmp_path) == snapshot.EXIT_FAILED
    run = read_run(tmp_path)
    assert run["status"] == "failed"
    assert run["categories"] == []
    assert token_file(tmp_path).read_text() == NEW_REFRESH


def _broken_highlights():
    broken = FakeML()
    for cat in ("MLA1055", "MLA1652"):
        broken.routes[f"/highlights/MLA/category/{cat}"] = lambda r: httpx.Response(503, json={})
    return broken


def _partial_fake():
    partial = FakeML()
    partial.routes["items:MLA2"] = lambda r: httpx.Response(500, json={})
    return partial


def test_failed_run_does_not_replace_a_usable_snapshot(fake, tmp_path):
    assert do_run(fake, tmp_path) == snapshot.EXIT_OK
    good_run_id = read_run(tmp_path)["run_id"]
    assert do_run(_broken_highlights(), tmp_path) == snapshot.EXIT_FAILED
    assert read_run(tmp_path)["run_id"] == good_run_id


def test_partial_run_does_not_replace_success(fake, tmp_path):
    do_run(fake, tmp_path)
    good = read_run(tmp_path)
    assert do_run(_partial_fake(), tmp_path) == snapshot.EXIT_OK
    assert read_run(tmp_path)["run_id"] == good["run_id"]
    assert read_run(tmp_path)["status"] == "success"


def test_partial_replaces_partial_and_failed(tmp_path):
    do_run(_broken_highlights(), tmp_path)
    assert read_run(tmp_path)["status"] == "failed"
    do_run(_partial_fake(), tmp_path)
    first_partial = read_run(tmp_path)
    assert first_partial["status"] == "partial"
    do_run(_partial_fake(), tmp_path)
    second_partial = read_run(tmp_path)
    assert second_partial["status"] == "partial"
    assert second_partial["run_id"] != first_partial["run_id"]


def test_success_replaces_success_atomically(fake, tmp_path):
    do_run(fake, tmp_path)
    first = read_run(tmp_path)
    (day_dir(tmp_path) / "stale.txt").write_text("left over")

    second_fake = FakeML()
    second_fake.offers["MLA58353028"] = [fx.offer(1)]
    do_run(second_fake, tmp_path)
    second = read_run(tmp_path)

    assert second["run_id"] != first["run_id"]
    assert not (day_dir(tmp_path) / "stale.txt").exists()
    assert {o["run_id"] for o in rows(tmp_path, "offers")} == {second["run_id"]}
    assert [p.name for p in base_dir(tmp_path).iterdir()] == [f"snapshot_date={DATE}"]


# --------------------------------------------------------------------------- storage hygiene


def test_stale_temp_and_backup_folders_are_cleaned(fake, tmp_path):
    base = base_dir(tmp_path)
    (base / ".tmp-2026-11-01-deadbeef").mkdir(parents=True)
    (base / ".tmp-2026-11-01-deadbeef" / "offers.jsonl.gz").write_bytes(b"half written")
    (base / "snapshot_date=2026-11-02").mkdir()
    (base / ".old-2026-11-02-cafe").mkdir()  # day folder exists -> backup is stale
    do_run(fake, tmp_path)
    names = sorted(p.name for p in base.iterdir())
    assert names == ["snapshot_date=2026-11-02", f"snapshot_date={DATE}"]


def test_backup_is_restored_when_day_folder_is_missing(fake, tmp_path):
    # Simulates a kill between the two renames: only the .old-* copy survives.
    base = base_dir(tmp_path)
    old = base / ".old-2026-11-01-cafe"
    old.mkdir(parents=True)
    (old / "run.json").write_text(json.dumps({"status": "success"}))
    do_run(fake, tmp_path)
    restored = base / "snapshot_date=2026-11-01"
    assert json.loads((restored / "run.json").read_text())["status"] == "success"
    assert not old.exists()


def test_storage_gitignore_written_and_preserved(fake, tmp_path):
    do_run(fake, tmp_path)
    gitignore = (tmp_path / "store" / ".gitignore").read_text().splitlines()
    assert ".tmp-*/" in gitignore and ".old-*/" in gitignore

    (tmp_path / "store" / ".gitignore").write_text("custom-rule\n")
    do_run(FakeML(), tmp_path)
    lines = (tmp_path / "store" / ".gitignore").read_text().splitlines()
    assert lines[0] == "custom-rule"
    assert ".tmp-*/" in lines and ".old-*/" in lines
    do_run(FakeML(), tmp_path)  # idempotent: no duplicate lines
    assert (tmp_path / "store" / ".gitignore").read_text().splitlines() == lines


def test_failed_swap_keeps_previous_snapshot(fake, tmp_path, monkeypatch, caplog):
    do_run(fake, tmp_path)
    good = read_run(tmp_path)
    real_rename = Path.rename

    def failing_rename(self, target):
        if self.name.startswith(".tmp-"):
            raise OSError("disk full")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)
    assert do_run(FakeML(), tmp_path) == snapshot.EXIT_FAILED
    assert "aborted in step write_snapshot" in caplog.text
    monkeypatch.undo()
    assert read_run(tmp_path)["run_id"] == good["run_id"]
    assert [p.name for p in base_dir(tmp_path).iterdir()] == [f"snapshot_date={DATE}"]


# --------------------------------------------------------------------------- cap, retry, secrets


def test_request_cap_keeps_offers_fetched_mid_pagination(fake, tmp_path):
    # trends, highlights MLA1055, product MLA58353028, items page 0 -> cap hit at page 1
    assert do_run(fake, tmp_path, max_requests=4) == snapshot.EXIT_OK
    run = read_run(tmp_path)
    assert run["requests_made"] == 4
    assert run["request_cap"] == 4
    assert run["request_cap_reached"] is True
    assert run["status"] == "partial"
    top = [o for o in rows(tmp_path, "offers") if o["product_id"] == "MLA58353028"]
    assert len(top) == 50  # first page kept
    assert len([p for p in fake.paths() if p != "/oauth/token"]) == 4


def test_retry_on_429_then_success(fake, tmp_path):
    statuses = iter([429, 200])

    def flaky(request):
        status = next(statuses)
        return httpx.Response(status, json=fx.trends(["tv"]) if status == 200 else {})

    fake.routes["/trends/MLA"] = flaky
    do_run(fake, tmp_path)
    assert read_run(tmp_path)["status"] == "success"
    assert fake.paths().count("/trends/MLA") == 2


def test_no_token_in_logs_or_output(fake, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    fake.routes["items:MLA2"] = lambda r: httpx.Response(500, json={})
    do_run(fake, tmp_path)
    for record in caplog.records:
        message = record.getMessage()
        for value in (ACCESS, OLD_REFRESH, NEW_REFRESH, SECRET):
            assert value not in message
    for path in day_dir(tmp_path).iterdir():
        raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
        for value in (ACCESS, OLD_REFRESH, NEW_REFRESH, SECRET):
            assert value.encode() not in raw


# --------------------------------------------------------------------------- CLI


def test_main_requires_env(monkeypatch, tmp_path):
    for name in ("ML_CLIENT_ID", "ML_CLIENT_SECRET", "ML_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    code = snapshot.main(["--out", str(tmp_path), "--new-refresh-token-file", str(tmp_path / "t")])
    assert code == snapshot.EXIT_CONFIG == 5


def test_main_parses_arguments(monkeypatch, tmp_path):
    monkeypatch.setenv("ML_CLIENT_ID", "app-id")
    monkeypatch.setenv("ML_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("ML_REFRESH_TOKEN", OLD_REFRESH)
    captured = {}
    monkeypatch.setattr(snapshot, "run", lambda **kw: captured.update(kw) or 0)
    code = snapshot.main(
        [
            "--out",
            str(tmp_path),
            "--date",
            "2026-11-01",
            "--categories",
            "MLA1",
            "MLA2",
            "--new-refresh-token-file",
            str(tmp_path / "t"),
        ]
    )
    assert code == 0
    assert captured["snapshot_date"] == "2026-11-01"
    assert captured["categories"] == ["MLA1", "MLA2"]
    assert captured["refresh_token"] == OLD_REFRESH
    assert captured["delay_s"] == 0.5


def test_main_defaults_to_five_verified_categories(monkeypatch, tmp_path):
    monkeypatch.setenv("ML_CLIENT_ID", "app-id")
    monkeypatch.setenv("ML_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("ML_REFRESH_TOKEN", OLD_REFRESH)
    captured = {}
    monkeypatch.setattr(snapshot, "run", lambda **kw: captured.update(kw) or 0)
    snapshot.main(["--out", str(tmp_path), "--new-refresh-token-file", str(tmp_path / "t")])
    assert captured["categories"] == ["MLA1055", "MLA1652", "MLA1002", "MLA109027", "MLA5726"]


def test_bad_date_rejected(tmp_path):
    with pytest.raises(SystemExit):
        snapshot.main(
            [
                "--out",
                str(tmp_path),
                "--date",
                "03/11/2026",
                "--new-refresh-token-file",
                str(tmp_path / "t"),
            ]
        )
