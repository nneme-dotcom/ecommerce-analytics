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
    "highlights": {"category_id", "position", "product_id"},
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
    "requests_made",
    "request_cap",
    "request_cap_reached",
    "row_counts",
    "errors",
    "status",
}


class FakeML:
    """Routes mocked requests: token endpoint + API paths. Override routes per test."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.token_response = httpx.Response(
            200,
            json={"access_token": ACCESS, "refresh_token": NEW_REFRESH, "expires_in": 21600},
        )
        many = [fx.offer(i) for i in range(120)]
        self.offers = {"MLA58353028": many, "MLA2": [fx.offer(500)], "MLA3": [fx.offer(600)]}
        self.routes = {
            "/trends/MLA": lambda r: httpx.Response(
                200, json=fx.trends(["iphone", "aire acondicionado"])
            ),
            "/highlights/MLA/category/MLA1055": lambda r: httpx.Response(
                200, json=fx.highlights(["MLA58353028", "MLA2"], extra_items=1)
            ),
            "/highlights/MLA/category/MLA1652": lambda r: httpx.Response(
                200,
                json=fx.highlights(["MLA3", "MLA2"]),  # MLA2 ranks in both categories
            ),
            "/products/MLA58353028": lambda r: httpx.Response(200, json=fx.product("MLA58353028")),
            "/products/MLA2": lambda r: httpx.Response(200, json=fx.product("MLA2", brand=None)),
            "/products/MLA3": lambda r: httpx.Response(200, json=fx.product("MLA3")),
        }

    def _items(self, request, product_id):
        q = parse_qs(request.url.query.decode())
        offset, limit = int(q["offset"][0]), int(q["limit"][0])
        all_offers = self.offers[product_id]
        page = all_offers[offset : offset + limit]
        return httpx.Response(200, json=fx.offers_page(page, offset, limit, len(all_offers)))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path == "/oauth/token":
            return self.token_response
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


def day_dir(tmp_path):
    return tmp_path / "store" / "mercadolibre" / f"snapshot_date={DATE}"


def read_jsonl(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def read_run(tmp_path):
    return json.loads((day_dir(tmp_path) / "run.json").read_text(encoding="utf-8"))


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
    assert run["errors"] == []
    assert run["request_cap_reached"] is False

    rows = {k: read_jsonl(folder / f"{k}.jsonl.gz") for k in CONTRACT_FIELDS}
    for kind, records in rows.items():
        assert records, kind
        for record in records:
            assert set(record) == CONTRACT_FIELDS[kind] | META_FIELDS, kind
            assert record["snapshot_date"] == DATE
            assert record["run_id"] == run["run_id"]
            assert record["captured_at"].endswith("Z")
    assert run["row_counts"] == {k: len(v) for k, v in rows.items()}

    # highlights: only type == PRODUCT; product ranked in two categories appears in both
    assert [(h["category_id"], h["position"], h["product_id"]) for h in rows["highlights"]] == [
        ("MLA1055", 1, "MLA58353028"),
        ("MLA1055", 2, "MLA2"),
        ("MLA1652", 1, "MLA3"),
        ("MLA1652", 2, "MLA2"),
    ]
    # products are deduplicated across categories and fetched once
    assert [p["product_id"] for p in rows["products"]] == ["MLA58353028", "MLA2", "MLA3"]
    assert fake.paths().count("/products/MLA2") == 1
    # trends keep 1-based order
    assert [(t["position"], t["keyword"]) for t in rows["trends"]] == [
        (1, "iphone"),
        (2, "aire acondicionado"),
    ]


def test_offers_are_paginated_and_capped(fake, tmp_path):
    do_run(fake, tmp_path, max_offers_per_product=100)
    offers = read_jsonl(day_dir(tmp_path) / "offers.jsonl.gz")
    top = [o for o in offers if o["product_id"] == "MLA58353028"]
    assert len(top) == 100  # 120 available, capped at 100
    assert len({o["item_id"] for o in top}) == 100
    pages = [
        parse_qs(c.url.query.decode())
        for c in fake.calls
        if c.url.path == "/products/MLA58353028/items"
    ]
    assert [(p["offset"][0], p["limit"][0]) for p in pages] == [("0", "50"), ("50", "50")]


def test_pagination_reads_all_pages_below_cap(fake, tmp_path):
    do_run(fake, tmp_path, max_offers_per_product=500)
    offers = read_jsonl(day_dir(tmp_path) / "offers.jsonl.gz")
    assert len([o for o in offers if o["product_id"] == "MLA58353028"]) == 120


def test_record_mapping_matches_probe_fields(fake, tmp_path):
    fake.offers["MLA3"] = [fx.offer(1, seller_address=None, official_store_id=55, deal_ids=None)]
    do_run(fake, tmp_path)
    folder = day_dir(tmp_path)
    products = {p["product_id"]: p for p in read_jsonl(folder / "products.jsonl.gz")}
    assert products["MLA58353028"]["brand"] == "Motorola"
    assert products["MLA58353028"]["model"] == "Moto G06"
    assert products["MLA2"]["brand"] is None
    # not confirmed by the probe -> nullable
    assert products["MLA3"]["family_name"] is None
    assert products["MLA3"]["permalink"] is None

    offers = read_jsonl(folder / "offers.jsonl.gz")
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


def test_rotated_token_written_to_file(fake, tmp_path):
    do_run(fake, tmp_path)
    token_file = tmp_path / "secrets" / "new_refresh_token"
    assert token_file.read_text(encoding="utf-8") == NEW_REFRESH
    form = parse_qs(fake.calls[0].content.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == [OLD_REFRESH]


def test_token_file_written_even_if_later_steps_crash(fake, tmp_path):
    def boom(request):
        raise RuntimeError("unexpected crash after refresh")

    fake.routes["/trends/MLA"] = boom
    with pytest.raises(RuntimeError):
        do_run(fake, tmp_path)
    assert (tmp_path / "secrets" / "new_refresh_token").read_text() == NEW_REFRESH
    assert not day_dir(tmp_path).exists()


def test_invalid_grant_exits_non_zero_and_writes_nothing(fake, tmp_path, caplog):
    fake.token_response = httpx.Response(400, json={"error": "invalid_grant"})
    assert do_run(fake, tmp_path) == snapshot.EXIT_AUTH
    assert not (tmp_path / "secrets" / "new_refresh_token").exists()
    assert not (tmp_path / "store").exists()
    assert "re-authorize" in caplog.text
    assert fake.paths() == ["/oauth/token"]


def test_token_persist_failure_exits_3_before_any_data_request(fake, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    code = do_run(fake, tmp_path, new_refresh_token_file=blocker / "token")
    assert code == snapshot.EXIT_TOKEN_PERSIST
    assert fake.paths() == ["/oauth/token"]


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
    offers = read_jsonl(day_dir(tmp_path) / "offers.jsonl.gz")
    assert "MLA2" not in {o["product_id"] for o in offers}
    assert "MLA3" in {o["product_id"] for o in offers}  # offers still fetched


def test_failed_when_no_highlights(fake, tmp_path):
    for cat in ("MLA1055", "MLA1652"):
        fake.routes[f"/highlights/MLA/category/{cat}"] = lambda r: httpx.Response(404, json={})
    assert do_run(fake, tmp_path) == snapshot.EXIT_FAILED
    run = read_run(tmp_path)
    assert run["status"] == "failed"
    assert (tmp_path / "secrets" / "new_refresh_token").read_text() == NEW_REFRESH


def test_failed_run_does_not_replace_a_usable_snapshot(fake, tmp_path):
    assert do_run(fake, tmp_path) == snapshot.EXIT_OK
    good_run_id = read_run(tmp_path)["run_id"]

    broken = FakeML()
    for cat in ("MLA1055", "MLA1652"):
        broken.routes[f"/highlights/MLA/category/{cat}"] = lambda r: httpx.Response(503, json={})
    assert do_run(broken, tmp_path) == snapshot.EXIT_FAILED
    assert read_run(tmp_path)["run_id"] == good_run_id
    assert read_run(tmp_path)["status"] == "success"


def test_rerun_same_date_overwrites_atomically(fake, tmp_path):
    do_run(fake, tmp_path)
    first = read_run(tmp_path)
    (day_dir(tmp_path) / "stale.txt").write_text("left over")

    second_fake = FakeML()
    second_fake.offers["MLA58353028"] = [fx.offer(1)]
    do_run(second_fake, tmp_path)
    second = read_run(tmp_path)

    assert second["run_id"] != first["run_id"]
    assert not (day_dir(tmp_path) / "stale.txt").exists()
    offers = read_jsonl(day_dir(tmp_path) / "offers.jsonl.gz")
    assert {o["run_id"] for o in offers} == {second["run_id"]}
    # no temp or backup folders left behind
    assert [p.name for p in (tmp_path / "store" / "mercadolibre").iterdir()] == [
        f"snapshot_date={DATE}"
    ]


def test_request_cap_gives_partial_snapshot(fake, tmp_path):
    assert do_run(fake, tmp_path, max_requests=4) == snapshot.EXIT_OK
    run = read_run(tmp_path)
    assert run["requests_made"] == 4
    assert run["request_cap"] == 4
    assert run["request_cap_reached"] is True
    assert run["status"] == "partial"
    # token call is not an API request; 4 API calls: trends, highlights, product, first offers page
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


def test_main_requires_env(monkeypatch, tmp_path):
    for name in ("ML_CLIENT_ID", "ML_CLIENT_SECRET", "ML_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    code = snapshot.main(["--out", str(tmp_path), "--new-refresh-token-file", str(tmp_path / "t")])
    assert code == snapshot.EXIT_AUTH


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


def test_main_defaults_to_five_categories(monkeypatch, tmp_path):
    monkeypatch.setenv("ML_CLIENT_ID", "app-id")
    monkeypatch.setenv("ML_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("ML_REFRESH_TOKEN", OLD_REFRESH)
    captured = {}
    monkeypatch.setattr(snapshot, "run", lambda **kw: captured.update(kw) or 0)
    snapshot.main(["--out", str(tmp_path), "--new-refresh-token-file", str(tmp_path / "t")])
    assert len(captured["categories"]) == 5
    assert captured["categories"][0] == "MLA1055"


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
