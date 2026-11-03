# Data contract

Single source of truth for every schema shared between agents. Change it **before** (or in the same PR as) the code.
Status per section: `draft` (may change freely) · `stable` (changes need a DECISIONS entry).

---

## 1. Mercado Libre raw snapshots — `draft`

One snapshot run per UTC day. A run is identified by `snapshot_date` (YYYY-MM-DD) and `run_id` (UUID).
Format: newline-delimited JSON (`.jsonl`), gzip-compressed. Layout (relative to the storage root, see ADR-003):

```
mercadolibre/snapshot_date=YYYY-MM-DD/
  run.json                  run metadata
  highlights.jsonl.gz       one line per (category, ranked product)
  products.jsonl.gz         one line per catalog product seen in the run
  offers.jsonl.gz           one line per offer per product
  trends.jsonl.gz           one line per trending keyword
```

Every line carries `snapshot_date`, `run_id` and `captured_at` (UTC ISO-8601, e.g. `2026-11-03T06:00:12Z`;
the time the source response was received — for offers, the first page of that product).

Writing rules (task 1.2):
- The day folder is written to `mercadolibre/.tmp-<date>-<run_id>/`, then swapped in by rename
  (old folder → `.old-<date>-<run_id>/`, new folder in, old folder deleted only after the new one is in
  place). Re-running a date replaces the whole folder (no merging). All five files are always present
  (a `.jsonl.gz` may be empty).
- **Replacement rule (ADR-007):** a run replaces the existing folder for the same date only if its status ranks
  at least as high: `success` > `partial` > `failed`. So `partial` never replaces `success`; `partial`
  may replace `partial` or `failed`; `failed` only replaces `failed`. With no existing folder, any run
  writes one (a `failed` one keeps the attempt visible). Empty data never counts as success: a
  category whose highlights come back empty is not captured (it is an `errors` entry).
- At run start, stale `.tmp-*` folders (killed runs) are deleted; a stale `.old-*` is deleted if its
  day folder exists, or **restored** if it doesn't (killed between the two renames). The job also
  makes sure `<storage root>/.gitignore` contains `.tmp-*/` and `.old-*/` (appending if needed), so
  those folders are never committed to the data repo.
- Highlights: **every** ranked entry is written (any `type`), deduplicated per (category, id), so
  ranking positions stay complete. Only `type == PRODUCT` entries are followed with product/offer
  calls; the others are counted in `run.json.skipped_highlights`.
- Products are deduplicated within a run (fetched once even if ranked in several categories).
- Offers: `/products/{id}/items` is paged with `offset`/`limit` (page size 50). The job never requests
  `offset >= paging.total` (the API answers 404). A 404 on the **first** page means the product has no
  offers (e.g. out of stock) and a 404 on a later page is end of data; neither is an error. Products
  with no offers (first-page 404, or 200 with `total: 0`) are counted in `products_without_offers`.
  A response without `paging` is a single page; pagination also stops when a page adds no new `item_id`. Offers are deduplicated by `item_id` per product and
  capped at 100 per product (`--max-offers-per-product`). If the request cap is hit mid-pagination,
  offers already fetched are kept (`request_cap_reached: true`).
- Malformed payloads never stop the run: wrong container types become null (objects/scalars) or `[]`
  (lists); an unexpected exception while handling trends, one category or one product is recorded in
  `errors` as `{path, status: null}` and the run continues.
- `?` = nullable. Fields not confirmed by docs/API_PROBE.md are read defensively and may be null.

### run.json
| field | type | notes |
|-------|------|-------|
| run_id | string (uuid) | |
| snapshot_date | date | |
| started_at / finished_at | timestamp UTC | |
| site_id | string | `MLA` |
| categories | string[] | category ids actually captured (highlights returned ≥1 entry) |
| categories_requested | string[] | category ids the run was asked for |
| requests_made | int | API GETs incl. retries (the token refresh is not counted) |
| request_cap | int | hard cap for this run |
| request_cap_reached | bool | collection stopped early because of the cap |
| row_counts | object | `{highlights, products, offers, trends}` line counts |
| skipped_highlights | object | per-`type` count of highlight entries written but not followed (e.g. `{"USER_PRODUCT": 9, "ITEM": 3}`); `UNKNOWN` = no type, `MALFORMED` = entries dropped for having no id |
| products_without_offers | int | followed products whose offers endpoint answered with no offers (first-page 404 or `total: 0`) — not errors |
| errors | object[] | `{path, status, count}` — no tokens, no payloads; `status` is null for network errors/timeouts, malformed or empty payloads and unexpected exceptions |
| status | string | `success` (≥1 category captured, no errors, cap not reached) · `partial` (≥1 category captured, but some errors or cap reached) · `failed` (no category captured: every highlights call failed or came back empty) |

### highlights
| field | type | source |
|-------|------|--------|
| category_id | string | request param |
| position | int? | `content[].position` |
| product_id | string | `content[].id` — a catalog product id when `type == PRODUCT`, otherwise the id of that type's entity |
| type | string? | `content[].type`: `PRODUCT` · `USER_PRODUCT` · `ITEM` (only `PRODUCT` is followed) |

### products
| field | type | source |
|-------|------|--------|
| product_id | string | `id` |
| name | string | `name` |
| domain_id | string | `domain_id` |
| family_name | string? | `family_name` (not seen in probe) |
| status | string? | `status` (not seen in probe) |
| brand | string? | `attributes[id=BRAND].value_name` |
| model | string? | `attributes[id=MODEL].value_name` |
| permalink | string? | `permalink` (not seen in probe) |

### offers
| field | type | source (`/products/{id}/items` → `results[]`) |
|-------|------|--------|
| product_id | string | request param |
| item_id | string | `item_id` |
| seller_id | int | `seller_id` |
| official_store_id | int? | `official_store_id` |
| price | decimal | `price` |
| original_price | decimal? | `original_price` |
| currency_id | string | `currency_id` |
| condition | string | `condition` |
| listing_type_id | string | `listing_type_id` |
| category_id | string | `category_id` |
| free_shipping | bool | `shipping.free_shipping` |
| logistic_type | string? | `shipping.logistic_type` |
| shipping_mode | string? | `shipping.mode` |
| deal_ids | string[] | `deal_ids` (null → `[]`) |
| tags | string[] | `tags` (null → `[]`) |
| seller_state | string? | `seller_address.state.name` (inner shape not confirmed by probe) |

### trends
| field | type | source |
|-------|------|--------|
| position | int | index in response (1-based) |
| keyword | string | `keyword` |
| url | string | `url` |

### Initial categories (`MLA`)
Default of `python -m ingestion.mercadolibre.snapshot` (override with `--categories`).
All five verified live on 2026-11-03 (each returned 20 highlights; see docs/API_PROBE.md).

| id | category (as named by the API) | verified |
|----|--------------------------------|----------|
| MLA1055 | Celulares y Smartphones | yes (2026-11-03) |
| MLA1652 | Notebooks | yes (2026-11-03) |
| MLA1002 | Televisores | yes (2026-11-03) |
| MLA109027 | Zapatillas | yes (2026-11-03) |
| MLA5726 | Electrodomésticos y Aires Ac. | yes (2026-11-03) |

---

## 2. Olist raw — `draft`

The nine CSVs of the public dataset, loaded unchanged into schema `raw_olist`:
customers, geolocation, order_items, order_payments, order_reviews, orders, products, sellers,
product_category_name_translation.

---

## 3. Warehouse (PostgreSQL) — `draft`

Layers: `raw_*` (as ingested) → `staging` (typed, cleaned, renamed) → `core` (star schema) → `marts` (metrics).
Detailed tables are defined in tasks 2.1–2.3.

---

## 4. Metric definitions — `draft`

Each metric served by the API is defined here (name, formula, grain, filters) in task 2.3.
