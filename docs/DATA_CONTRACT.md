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

## 2b. Enrichment raw — `draft`

Schema `raw_enrichment`, written by task 1.5. Both sources are public and unauthenticated. Unlike
`raw_olist`, values arrive as JSON and are stored **typed** (dates, numerics, booleans, arrays).
Source shapes are documented assumptions (ADR-012): neither API is reachable from the agents'
workspace; the first live run is the manual workflow `enrichment-load.yml`.

### `raw_enrichment.fx_rates` (`python -m ingestion.enrichment.fx_rates`)

ECB euro foreign exchange reference rates, one row per ECB publication day and currency
(no rows on weekends or TARGET holidays: carry-forward is a `staging` decision).
Source: Frankfurter **v1** (`https://api.frankfurter.dev/v1/<start>..<end>?from=EUR&to=BRL,USD`),
which serves ECB data only.

| column | type | notes |
|--------|------|-------|
| rate_date | date | ECB reference date |
| base_currency | char(3) | always `EUR` |
| quote_currency | char(3) | `BRL`, `USD` |
| rate | numeric (> 0) | units of quote currency per 1 EUR, exact as published (no float rounding) |
| source | text | `ECB via Frankfurter v1` |

PK (`rate_date`, `base_currency`, `quote_currency`). Default coverage: 2016-09-01 → today (UTC).
USD→BRL etc. are derived in `staging` (cross rate = BRL/USD on the same date).
**ARS is not available**: the ECB does not publish an ARS reference rate, and the loader refuses
`--quotes ARS`. ARS (needed to convert Mercado Libre prices) is left for a later task; a candidate
source is BCRA (Frankfurter v2 exposes it as provider `BCRA`), not decided.
Load rule: one transaction deletes the requested date range × currencies and inserts the fetched
rows (idempotent; picks up revisions). Frankfurter snaps a range start that is not a publication
day back to the previous one (a request from 2024-01-01 starts on 2023-12-29). Such leading dates
are dropped, because the previous yearly chunk already covers them, but only up to 7 calendar days
before the range start; an older date, or a date after the range end, is an error. A chunk (one
calendar year per request) must be dense **for each currency** (≥ 80 % of weekdays have a rate
for that `quote_currency`) or the run fails; an empty answer never deletes data.

### `raw_enrichment.public_holidays` (`python -m ingestion.enrichment.holidays`)

One row per holiday entry as returned by Nager.Date **v3**
(`https://date.nager.at/api/v3/PublicHolidays/{year}/{countryCode}`; ADR-013. v3 is end-of-life
on 2027-01-31, and migrating to v4 is PLAN 1.7). All holiday types are kept (`Public`, `Bank`,
`School`, `Authorities`, `Optional`, `Observance`); filter in `staging`.

| column | type | source field | rule |
|--------|------|--------------|------|
| country_code | char(2) | `countryCode` | required, must equal the requested country |
| holiday_date | date | `date` | required, `YYYY-MM-DD` inside the requested year |
| local_name | text? | `localName` | string or null |
| name | text | `name` | required (English name) |
| is_global | boolean | `global` | required; `true` = nationwide |
| counties | text[]? | `counties` | ISO 3166-2 codes (e.g. `BR-SP`) or null (nationwide) |
| holiday_types | text[] | `types` | required, non-empty |
| is_fixed | boolean? | `fixed` | boolean or null/absent |
| launch_year | int? | `launchYear` | integer or null |
| source | text | — | `Nager.Date v3` |

No primary key: rows are raw as returned, and the same date may appear for different counties or
types. Any entry breaking a rule fails the whole run with nothing loaded, so a changed payload
(e.g. the v4 field names) cannot load silently.
Default coverage: **BR 2016–2018** (Olist years) and **AR 2026 → next year** (Mercado Libre years);
override with `--only CC:YYYY[-YYYY]`. Load rule: one transaction deletes the requested
(country, year) pairs and inserts the fetched rows. A (country, year) that answers with no entries
fails the run and nothing changes.

Schema drift (columns' names, order or types differ from this contract): each loader re-creates
**only its own table** (`fx_rates` or `public_holidays`) inside the load transaction (no
`CASCADE`). Any other missing table is created; another drifted data table is left untouched
(warning logged). `_load_audit` is **never dropped**: if it drifts, the load fails (exit 1) and the
transaction rolls back with nothing changed; migrate it by hand.

### `raw_enrichment._load_audit` (append-only)

| column | type | notes |
|--------|------|-------|
| load_id | uuid | one per run; PK (`load_id`, `table_name`) |
| table_name | text | `fx_rates` or `public_holidays` |
| row_count | bigint | rows inserted by this run |
| params | jsonb | fx: `{start, end, base, quotes, source}`; holidays: `{countries_years: ["BR:2016", ...], source}` |
| loaded_at | timestamptz | transaction time |

---

## 3. Warehouse (PostgreSQL) — `draft`

Layers: `raw_*` (as ingested) → `staging` (typed, cleaned, renamed) → `core` (star schema) → `marts` (metrics).
Detailed tables are defined in tasks 2.1–2.3.

### Schemas and ownership (task 2.0, ADR-014, ADR-016)

| schema | written by | materialisation | contents |
|--------|-----------|-----------------|----------|
| `raw_olist`, `raw_enrichment` (`raw_mercadolibre` from 2.5) | Python loaders only | tables | §1b, §2, §2b; dbt reads them as **sources** and never writes to them |
| `staging` | dbt (`warehouse/models/staging/`) | views | one model per raw entity: typed, renamed, deduplicated |
| `core` | dbt (`warehouse/models/core/`) | tables | star schema (2.1, 2.2) |
| `marts` | dbt (`warehouse/models/marts/`) | tables | metrics (2.3, §4); the only schema the 2.4 API reads |

dbt creates **exactly** these three schemas: `generate_schema_name` (warehouse/macros/) uses the
folder's `+schema` verbatim (no `<target>_` prefix) and fails parsing for a model, seed or
snapshot whose schema is anything else, for a target schema outside the three, and for any node
that sets the legacy `target_schema` / `target_database` (which would bypass the macro). The
profile's target schema is `staging`, so even test failure tables (`--store-failures`) land there.
The `on-run-start` hook `assert_node_schemas` re-checks every node's final schema and database
before anything runs. CI checks after `dbt build` that no other schema appeared and that nothing
in the `raw_*` schemas or `public` changed: relations (columns, types, row count, md5 of the rows),
constraints, triggers, functions, types (ADR-016).

Naming: `stg_<source>__<entity>` (sources `olist`, `enrichment`), `dim_<entity>`, `fact_<process>`.
Every model states its grain in a leading comment and in its YAML description.

### Sources

Declared in `warehouse/models/staging/<source>/_<source>__sources.yml` with **every** column of the
raw table, in table order, with its type (`data_type`, required) as printed by PostgreSQL
`format_type()` (`text`, `character(3)`, `text[]`, ...). The singular test
`assert_sources_match_contract` fails if a declared table's columns, order or types differ from the
database, or if a column has no `data_type`, so the YAML, the loader DDL and §2/§2b must change
together.

| dbt source | schema | tables |
|------------|--------|--------|
| `olist` | `raw_olist` | the nine tables of §2 + `_load_audit` |
| `enrichment` | `raw_enrichment` | `fx_rates`, `public_holidays`, `_load_audit` |

Source tests (only what §2/§2b guarantee): `raw_olist` data tables have none (no keys or
constraints in raw; key tests start in `staging`). `raw_enrichment.fx_rates`: unique
(`rate_date`, `base_currency`, `quote_currency`), not null, `base_currency` = `EUR`, `rate` > 0,
`source` = `ECB via Frankfurter v1`, `quote_currency` in (`BRL`, `USD`) as a **warning** (the
loader accepts other ECB currencies via `--quotes`). `public_holidays`: not null on the required
fields, `source` = `Nager.Date v3` (fails on the v4 migration until staging is updated, PLAN 1.7).
Both `_load_audit` tables: unique (`load_id`, `table_name`), not null, `table_name` in the
loader's tables.

### `staging` models

| model | grain | columns | notes |
|-------|-------|---------|-------|
| `stg_olist__product_category_translation` | one row per `product_category_name` | `product_category_name` text, `product_category_name_english` text | from `raw_olist.product_category_name_translation`, unchanged; tests: `product_category_name` unique + not null, English name not null. Task 2.0 smoke model |

---

## 4. Metric definitions — `draft`

Each metric served by the API is defined here (name, formula, grain, filters) in task 2.3.
