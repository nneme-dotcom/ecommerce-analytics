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

Source: the Kaggle dataset `olistbr/brazilian-ecommerce` ("Brazilian E-Commerce Public Dataset by
Olist"), licensed CC BY-NC-SA 4.0 per the Kaggle page. Raw CSVs are never committed (`data/` is
git-ignored); attribution is in the README.

Headers and record counts below were **verified on 2026-11-03** against the official Kaggle archive
(`archive.zip`, 44,717,580 bytes) by a full local load. The loader refuses a file whose header
differs and prints the header it found; if the upstream files ever change, update this table and
`ingestion/olist/files.py` in the same PR. CI uses synthetic fixtures with the same headers
(`tests/olist/fixtures/`); `tests/olist/test_olist_real_data.py` checks the real counts when
`OLIST_SOURCE_DIR` is set.

### Files on disk (task 1.4, `python -m ingestion.olist.download`)

```
data/olist/                          (default --dest; git-ignored)
  <the nine CSVs below>              extracted unchanged from the archive
  manifest.json                      {dataset, source: "kaggle-api"|"zip", archive_sha256,
                                      created_at, files: {<csv name>: {sha256, bytes}}}
```

The archive must contain **exactly** the nine CSVs, at its root (anything else, missing or duplicated
entries → refused, nothing written). Files are moved into place only after all of them extracted
with a valid CRC; `manifest.json` is written last.

### Schema `raw_olist` (task 1.4, `python -m ingestion.olist.load`)

Rules:
- One table per CSV, named below. **Every column is `text`**, named exactly as the CSV header and in
  the same order (upstream typos kept). No keys,
  no constraints, no type casts: typing, renaming and dedup happen in `staging` (task 2.1).
- Values are loaded unchanged with `COPY ... (FORMAT csv, HEADER)`: an **unquoted empty field is
  NULL**, a quoted empty string (`""`) is `''`. Multi-line quoted values (review comments) keep their
  line breaks (`\r\n` included). A UTF-8 byte-order mark before the header is tolerated.
- Row count of each table = number of CSV **records** (parsed as CSV, header excluded), checked
  inside the load; a mismatch aborts.
- Idempotent: the whole load (nine tables + audit rows) is one transaction. Each table is
  truncated and reloaded; if its columns no longer match this contract it is dropped and re-created
  (no `CASCADE`: a dependent view makes the load fail rather than silently disappear).

| table | file | columns | rows (verified) |
|-------|------|---------|----------------|
| customers | olist_customers_dataset.csv | customer_id, customer_unique_id, customer_zip_code_prefix, customer_city, customer_state | 99,441 |
| geolocation | olist_geolocation_dataset.csv | geolocation_zip_code_prefix, geolocation_lat, geolocation_lng, geolocation_city, geolocation_state | 1,000,163 |
| order_items | olist_order_items_dataset.csv | order_id, order_item_id, product_id, seller_id, shipping_limit_date, price, freight_value | 112,650 |
| order_payments | olist_order_payments_dataset.csv | order_id, payment_sequential, payment_type, payment_installments, payment_value | 103,886 |
| order_reviews | olist_order_reviews_dataset.csv | review_id, order_id, review_score, review_comment_title, review_comment_message, review_creation_date, review_answer_timestamp | 99,224 |
| orders | olist_orders_dataset.csv | order_id, customer_id, order_status, order_purchase_timestamp, order_approved_at, order_delivered_carrier_date, order_delivered_customer_date, order_estimated_delivery_date | 99,441 |
| products | olist_products_dataset.csv | product_id, product_category_name, product_name_lenght, product_description_lenght, product_photos_qty, product_weight_g, product_length_cm, product_height_cm, product_width_cm | 32,951 |
| sellers | olist_sellers_dataset.csv | seller_id, seller_zip_code_prefix, seller_city, seller_state | 3,095 |
| product_category_name_translation | product_category_name_translation.csv | product_category_name, product_category_name_english | 71 |

Upstream quirks kept verbatim in raw (rename/handle in `staging`):
`product_category_name_translation.csv` starts with a UTF-8 BOM (stripped from the column name);
`products` has the typos `product_name_lenght` / `product_description_lenght` (rename to `*_length`
in staging); 3,852 review messages contain line breaks.

Other known quirks for `staging` (from the dataset description, not yet checked): `geolocation` has many
rows per zip prefix and is not unique; `order_reviews.review_id` is not unique on its own;
`customer_id` is per order, `customer_unique_id` is the person.

### `raw_olist._load_audit` (append-only, one row per table per load)

| column | type | notes |
|--------|------|-------|
| load_id | uuid | one per `load` run; PK is (`load_id`, `table_name`) |
| table_name | text | e.g. `orders` |
| source_file | text | CSV file name (no path) |
| row_count | bigint | rows loaded (= CSV records) |
| file_sha256 | char(64) | SHA-256 of the CSV loaded |
| loaded_at | timestamptz | transaction time (same for all tables of a load) |

Latest load per table: `SELECT DISTINCT ON (table_name) * FROM raw_olist._load_audit ORDER BY table_name, loaded_at DESC`.

---

## 3. Warehouse (PostgreSQL) — `draft`

Layers: `raw_*` (as ingested) → `staging` (typed, cleaned, renamed) → `core` (star schema) → `marts` (metrics).
Detailed tables are defined in tasks 2.1–2.3.

---

## 4. Metric definitions — `draft`

Each metric served by the API is defined here (name, formula, grain, filters) in task 2.3.
