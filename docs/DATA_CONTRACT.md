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
- The day folder is written to a temp folder and swapped in by rename, so re-running a date replaces
  the whole folder (no merging). All five files are always present (a `.jsonl.gz` may be empty).
- A `failed` run never replaces an existing `success`/`partial` folder for the same date; if there is
  none, it writes the folder with `status: failed` so the attempt is visible.
- Products are deduplicated within a run (fetched once even if ranked in several categories);
  highlights keep one line per (category, product).
- Offers: `/products/{id}/items` is paged with `offset`/`limit` (page size 50) until `paging.total`,
  capped at 100 offers per product (`--max-offers-per-product`).
- `?` = nullable. Fields not confirmed by docs/API_PROBE.md are read defensively and may be null.

### run.json
| field | type | notes |
|-------|------|-------|
| run_id | string (uuid) | |
| snapshot_date | date | |
| started_at / finished_at | timestamp UTC | |
| site_id | string | `MLA` |
| categories | string[] | category ids captured |
| requests_made | int | API GETs incl. retries (the token refresh is not counted) |
| request_cap | int | hard cap for this run |
| request_cap_reached | bool | collection stopped early because of the cap |
| row_counts | object | `{highlights, products, offers, trends}` line counts |
| errors | object[] | `{path, status, count}` — no tokens, no payloads; `status` is null for network errors/timeouts |
| status | string | `success` (no errors) · `partial` (some errors or cap reached, data written) · `failed` (no highlights for any category) |

### highlights
| field | type | source |
|-------|------|--------|
| category_id | string | request param |
| position | int | `content[].position` |
| product_id | string | `content[].id` (only `type == PRODUCT`) |

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
Only `MLA1055` is verified (probe). **The other four are to be validated against `/sites/MLA/categories`
in the first live run** — a wrong id shows up as a 404 on `/highlights/MLA/category/{id}` in `run.json.errors`.

| id | intended category | verified |
|----|-------------------|----------|
| MLA1055 | Cell phones (Celulares y Smartphones) | yes (probe) |
| MLA1652 | Notebooks | to be validated |
| MLA1002 | TVs (Televisores) | to be validated |
| MLA109027 | Sneakers (Zapatillas) | to be validated |
| MLA5726 | Home appliances (Electrodomésticos) | to be validated |

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
