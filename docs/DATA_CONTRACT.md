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

The storage root is the private data repo `nneme-dotcom/ecommerce-analytics-data` (`main`). Its
top level holds `mercadolibre/` (above), `.gitignore` (written by the job) and `olist/archive.zip`
(the Olist source archive, §2; added by the owner, read by the warehouse workflow, task 2.6).

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

## 1b. Mercado Libre raw in PostgreSQL — `draft`

Schema `raw_mercadolibre`, written by task 2.5 (`python -m ingestion.mercadolibre.load --source
<data-repo-root>`, env `DATABASE_URL`). It mirrors the §1 day folders of the private data repo, which
stays the system of record (ADR-003, ADR-014): the schema can be dropped and rebuilt from it at any
time. One table per snapshot file, plus `runs` (from `run.json`) and `_load_audit`.

Typed columns are exactly the §1 fields; no field is added or derived. **`payload` (jsonb) holds the
full line, parsed as jsonb (values exact; whitespace, key order and duplicate keys are not kept)**
(the run.json object, for `runs`), so no value is lost if a typed column is null. Loader-added
columns: `line_no` (1-based line in the `.jsonl.gz`), and `load_id`/`file_sha256` in `runs`.

### Load rules
- **Every** `mercadolibre/snapshot_date=YYYY-MM-DD/` folder under `--source` is validated before the
  database is touched; `.tmp-*`/`.old-*` leftovers are skipped with a warning. Days of any `status`
  (incl. `failed`) are loaded; filter on `runs.status` downstream. The ADR-007 replacement rule
  already happened in the data repo: the loader takes each day folder as it is.
- **Validation** (any failure = exit 1, nothing changed): `run.json` present, a JSON object with
  `run_id` (uuid), `snapshot_date` (= folder date), `status` (`success`·`partial`·`failed`) and
  `row_counts` (the four files, non-negative ints); all four `.jsonl.gz` files present and valid gzip;
  every line UTF-8 JSON object (no blank lines; NaN/Infinity, NUL characters and lone UTF-16
  surrogates such as a `\udc80` escape refused, in any key or value; a zero-byte file is not valid
  gzip) with `snapshot_date` = folder date, `run_id` = run.json `run_id`, `captured_at` an ISO-8601
  timestamp with offset, and the required fields below; line count = `row_counts[<file>]`. Error
  messages name the file and line, **never the line content** (Actions logs of this repo are public).
- **Typed columns are lenient:** a value that is absent or of the wrong JSON type (e.g. a string
  `price`, a bool where an int is expected, an int outside the column range) is loaded as NULL; the
  original stays in `payload`. Numbers are parsed as exact decimals (no float rounding).
- **Idempotent per `snapshot_date`, one transaction for the whole run:** for each day to load, delete
  that date from the five tables, `COPY` the files, check that the loaded row count per table equals
  `row_counts` (runs: 1), append the audit rows. Any failure rolls the whole run back.
- **Unchanged days are skipped (ADR-015):** a day whose five file SHA-256s equal `runs.file_sha256`
  of the loaded copy is not rewritten (no delete, no insert, no audit row). `--full-reload` reloads
  every day; a data table that was just created or re-created forces a full reload too. A re-run day
  (new files in the data repo) is therefore replaced on the next load.
- **Orphan days** (dates present in any of the five data tables but with no folder under
  `--source`) are kept (warning logged). They are read from all five tables before anything is
  dropped. **Orphans + drift = failure:** if a data table must be created or re-created (drift below,
  or dropped by hand) while orphans exist, the run fails (exit 1, nothing changed) because their
  rows in that table would be lost; run again with `--source` set to the full data repo checkout. A
  drifted table that holds rows but has no `snapshot_date date` column also fails the run (its days
  are unknown): drop it by hand.
- **Schema drift** (columns' names, order or types differ from this section): every data table here
  (`runs`, `highlights`, `products`, `offers`, `trends`) is owned by this loader and is dropped and
  re-created inside the load transaction (no `CASCADE`: a dependent view makes the load fail).
  `_load_audit` is checked **first** and **never dropped**: if it drifts the load fails (exit 1) with
  nothing changed; migrate it by hand. Constraints (PK, NOT NULL) are not part of the drift check.
- Exit codes: 0 ok (incl. nothing to reload) · 1 validation or load failed (nothing changed) · 2
  configuration error (`DATABASE_URL` missing). `DATABASE_URL` is never logged; database data errors
  and unexpected exceptions are logged by class/SQLSTATE only, never with their message.

### Common columns (the four file tables, in this order)
| column | type | notes |
|--------|------|-------|
| snapshot_date | date | required, = folder date |
| run_id | uuid | required, = run.json `run_id` |
| line_no | integer | 1-based line in the file; PK (`snapshot_date`, `line_no`) |
| captured_at | timestamptz | required |
| *typed fields* | | per table below |
| payload | jsonb | the full line, parsed as jsonb |

### `raw_mercadolibre.highlights`
`category_id` text (required) · `position` integer? · `product_id` text (required) · `type` text?

### `raw_mercadolibre.products`
`product_id` text (required) · `name` text? · `domain_id` text? · `family_name` text? · `status`
text? · `brand` text? · `model` text? · `permalink` text?

### `raw_mercadolibre.offers`
`product_id` text (required) · `item_id` text? · `seller_id` bigint? · `official_store_id` bigint? ·
`price` numeric? · `original_price` numeric? · `currency_id` text? · `condition` text? ·
`listing_type_id` text? · `category_id` text? · `free_shipping` boolean? · `logistic_type` text? ·
`shipping_mode` text? · `deal_ids` text[]? · `tags` text[]? · `seller_state` text?

`item_id` is not a key in raw (the writer keeps an offer without `item_id`); one row per offer per
product per day. Uniqueness of (`snapshot_date`, `product_id`, `item_id`) is a `staging` test.

### `raw_mercadolibre.trends`
`position` integer (required) · `keyword` text? · `url` text?

### `raw_mercadolibre.runs` (one row per loaded day)
| column | type | notes |
|--------|------|-------|
| snapshot_date | date | PK |
| run_id | uuid | required |
| status | text | required: `success` · `partial` · `failed` |
| started_at, finished_at | timestamptz? | |
| site_id | text? | |
| categories, categories_requested | text[]? | |
| requests_made, request_cap | integer? | |
| request_cap_reached | boolean? | |
| row_counts | jsonb | required |
| skipped_highlights | jsonb? | |
| products_without_offers | integer? | |
| errors | jsonb? | |
| payload | jsonb | the full run.json |
| load_id | uuid | load that wrote this day |
| file_sha256 | jsonb | `{"run.json": <sha256>, "highlights.jsonl.gz": ..., ...}` of the loaded files |

### `raw_mercadolibre._load_audit` (append-only, one row per table per day loaded)
| column | type | notes |
|--------|------|-------|
| load_id | uuid | one per run; PK (`load_id`, `snapshot_date`, `table_name`) |
| snapshot_date | date | |
| table_name | text | `runs`, `highlights`, `products`, `offers`, `trends` |
| run_id | uuid | snapshot run that produced the files |
| source_file | text | e.g. `offers.jsonl.gz` (no path) |
| row_count | bigint | rows loaded |
| file_sha256 | char(64) | SHA-256 of the file loaded |
| loaded_at | timestamptz | transaction time |

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

### The archive in the data repo (task 2.6, ADR-014, ADR-017)

The Kaggle `archive.zip` (44,717,580 bytes on 2026-11-03) is stored once by the owner in the
private data repo `nneme-dotcom/ecommerce-analytics-data` at **`olist/archive.zip`** (a regular git
file, not Git LFS), next to `mercadolibre/`. It is never copied into this public repo. The warehouse
workflow unpacks it with `--from-zip` into `$RUNNER_TEMP/olist` (the manifest above, with
`archive_sha256`, is written there), logs its size and SHA-256 and shows `archive_sha256` in the
job summary; the per-CSV SHA-256s land in `raw_olist._load_audit` (the durable record). The next
daily run reloads `raw_olist` when the SHA-256 of any of the nine CSVs inside the archive differs
from its latest `_load_audit.file_sha256`: a new commit to that path that changes a CSV triggers
it, while repacking the archive with byte-identical CSVs (a different archive hash) does not.

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
| `staging` | dbt (`warehouse/models/staging/`) | views | one model per raw entity: typed, renamed, cleaned; one row per raw record (aggregation to a coarser grain, e.g. geolocation, happens in `core`) |
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

### The warehouse on Neon (task 2.6, ADR-014, ADR-017)

One Neon project (EU, Frankfurt; free plan), one database, reached only from the GitHub Actions
workflow `warehouse-load.yml` through the Environment `warehouse` secret `WAREHOUSE_DATABASE_URL`
(direct, non-pooled, `sslmode=require`, optionally `channel_binding=require`). The database holds
exactly the six schemas of the table above plus PostgreSQL's own (`public` stays empty):

| schema | loaded by (workflow step) | daily run | `full` run |
|--------|---------------------------|-----------|------------|
| `raw_mercadolibre` | `python -m ingestion.mercadolibre.load --source <data repo>` | always (unchanged days skipped, ADR-015) | always |
| `raw_olist` | `ingestion.olist.download --from-zip <data repo>/olist/archive.zip`, then `ingestion.olist.load` | only if a CSV in the archive differs from the last `_load_audit` hash, or never loaded | always |
| `raw_enrichment` | `ingestion.enrichment.fx_rates`, `ingestion.enrichment.holidays` (defaults) | only if never loaded, or `public_holidays` has no `AR` row for the current UTC year | always |
| `staging`, `core`, `marts` | `dbt build --project-dir warehouse --profiles-dir warehouse` | after the loads | after the loads |

Daily runs are chained after each successful *ML daily snapshot* run; `full` is a manual dispatch.
The Olist and enrichment loads run even if the ML load failed; `dbt build` runs only if every load
that was attempted succeeded, and any failed load fails the run (ADR-017).
The job summary reports row counts per raw table, ML days loaded, dbt result counts and
`pg_database_size` (counts only). Every raw schema can be dropped and rebuilt with one `full` run.

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

### `staging` models (task 2.1)

Views, one per raw table, **one row per raw record** (no filter, no dedup, no join). Rules, the
same for every Olist model:
- **Empty string → NULL** on every column (`nullif(col, '')`): raw keeps a quoted `""` as `''`
  (§2), e.g. an empty review title.
- **Types:** ids and names `text`; timestamps `timestamp` (without time zone: Olist times are
  local Brazilian time and carry no offset); money `numeric` (BRL, exact); counts and
  measures `integer` (product measures are cast through `numeric`, so `40.0` loads);
  lat/lng `double precision`.
- **Renames:** only the upstream typos `product_name_lenght` → `product_name_length` and
  `product_description_lenght` → `product_description_length`; every other column keeps its §2
  name. Descriptive names (`*_at`, `*_brl`) start in `core`.
- **Zip prefixes** (`customer_`/`seller_`/`geolocation_zip_code_prefix`): a value shorter than 5
  characters is left-padded with zeros (macro `zip_prefix`; never truncated) and every value
  is tested against `^[0-9]{5}$`. The real CSVs already have 5 digits; this protects the joins.
- No business logic: statuses, NULL timestamps and NULL categories pass through as they are.

| model | grain (key) | columns (type) | tests |
|-------|-------------|----------------|-------|
| `stg_olist__customers` | `customer_id` | `customer_id`, `customer_unique_id`, `customer_zip_code_prefix`, `customer_city`, `customer_state` (all text) | `customer_id` unique + not null; `customer_unique_id`, city not null; zip format; state in the 27 UF codes |
| `stg_olist__geolocation` | none: one row per raw record (many rows per prefix, exact duplicates) | `geolocation_zip_code_prefix` text, `geolocation_lat`, `geolocation_lng` double precision, `geolocation_city`, `geolocation_state` text | all not null; zip format; state in the 27 UF codes |
| `stg_olist__order_items` | (`order_id`, `order_item_id`) | `order_id`, `order_item_id` integer, `product_id`, `seller_id`, `shipping_limit_date` timestamp, `price`, `freight_value` numeric | key unique; all not null; `order_item_id`, `price` > 0; `freight_value` ≥ 0 |
| `stg_olist__order_payments` | (`order_id`, `payment_sequential`) | `order_id`, `payment_sequential` integer, `payment_type`, `payment_installments` integer, `payment_value` numeric | key unique; not null; `order_id` → `stg_olist__orders`; `payment_type` in (`credit_card`, `boleto`, `voucher`, `debit_card`, `not_defined`); installments, value ≥ 0 |
| `stg_olist__order_reviews` | (`review_id`, `order_id`): `review_id` alone is **not** unique, and an order can have several reviews | `review_id`, `order_id`, `review_score` integer, `review_comment_title`, `review_comment_message`, `review_creation_date`, `review_answer_timestamp` timestamp | key unique; `review_id`, `order_id`, creation date not null; `order_id` → orders; score in 1–5 |
| `stg_olist__orders` | `order_id` | `order_id`, `customer_id`, `order_status`, `order_purchase_timestamp`, `order_approved_at`, `order_delivered_carrier_date`, `order_delivered_customer_date`, `order_estimated_delivery_date` (timestamps; the estimate is a date at 00:00) | `order_id` unique + not null; `customer_id` unique (one per order) + not null + → customers; status in (`approved`, `canceled`, `created`, `delivered`, `invoiced`, `processing`, `shipped`, `unavailable`); purchase and estimate not null |
| `stg_olist__products` | `product_id` | `product_id`, `product_category_name` (NULL for some), `product_name_length`, `product_description_length`, `product_photos_qty`, `product_weight_g`, `product_length_cm`, `product_height_cm`, `product_width_cm` (integer) | `product_id` unique + not null; measures ≥ 0 |
| `stg_olist__sellers` | `seller_id` | `seller_id`, `seller_zip_code_prefix`, `seller_city`, `seller_state` (text) | `seller_id` unique + not null; zip format; city not null; state in the 27 UF codes |
| `stg_olist__product_category_translation` | `product_category_name` | `product_category_name`, `product_category_name_english` (text) | name unique + not null, English not null. The loader already strips the CSV's BOM |
| `stg_enrichment__fx_rates` | (`rate_date`, `base_currency`, `quote_currency`) | `rate_date` date, `base_currency`, `quote_currency` text, `rate` numeric | key unique; not null; base = `EUR`; `rate` > 0. No rows on weekends/TARGET holidays (carry-forward is in `core`) |
| `stg_enrichment__public_holidays` | none: one row per raw entry | `country_code` text, `holiday_date` date, `local_name`, `holiday_name` (= raw `name`), `is_global`, `counties`, `holiday_types`, `is_fixed`, `launch_year` | required fields not null |

### `core` models — Olist star schema (task 2.1, ADR-018)

Tables. **Keys are Olist's natural keys** (32-hex ids, 5-digit zip prefixes) and the calendar
date itself (`date`), used the same way in every fact and dimension; no surrogate keys (ADR-018).
Row counts below are the expected ones on the full real load (from the §2 counts and the
public dataset's known distinct counts); the reconciliation tests enforce the ones derived from
raw.

| model | grain (key) | expected real rows | built from |
|-------|-------------|--------------------|------------|
| `dim_date` | `date_day` | 1,096 (2016-01-01 .. 2018-12-31) | order timestamps, `stg_enrichment__public_holidays` |
| `dim_customer` | `customer_unique_id` | 96,096 | customers + orders |
| `dim_seller` | `seller_id` | 3,095 | sellers |
| `dim_product` | `product_id` | 32,951 | products + translation |
| `dim_geo` | `zip_code_prefix` | ≈ 19.2k (19,015 geolocation prefixes + the customer/seller prefixes geolocation lacks) | geolocation (1,000,163 rows), customers, sellers |
| `fact_order_items` | (`order_id`, `order_item_id`) | 112,650 (= raw `order_items`) | order items + orders + customers + FX |
| `fact_deliveries` | `order_id` | 99,441 (= raw `orders`) | orders + customers |

Storage, measured on the real load: ≈ 70 MB for all of `core` (the facts dominate), whole
database ≈ 253 MB, against Neon's 1 GB (ADR-014). Raw geolocation is never copied into `core`.

**`dim_date`**: every day from 1 January of the first year to 31 December of the last year in
which any of the five order timestamps falls. Columns: `date_day` date, `iso_year`,
`calendar_year`, `calendar_quarter`, `calendar_month` integer, `year_month` text (`YYYY-MM`),
`month_name` text (English), `iso_week`, `iso_day_of_week` integer (1 = Monday), `day_name` text
(English), `is_weekend` boolean, `is_br_public_holiday` boolean, `br_holiday_name` text.
`is_br_public_holiday` is true on a **nationwide public holiday**: a `BR` entry with
`is_global` and `'Public' = any(holiday_types)` (regional entries with `counties`, and
Optional/Bank/Observance types, never count); names of several entries on one date are joined
with `; `. It is **NULL in a year with no BR row** in the holiday calendar (unknown, not false;
BR is loaded for 2016–2018). Tests: `date_day` unique + not null; contiguous whole years;
the flag follows the calendar rule (singular `assert_dim_date_holiday_flag_matches_calendar`).

**`dim_customer`**: `customer_unique_id` text, `customer_zip_code_prefix`, `customer_city`,
`customer_state` text, `customer_id_count` bigint, `first_purchase_at`, `last_purchase_at`
timestamp. **`customer_id` vs `customer_unique_id`:** Olist issues a new `customer_id` for every
order (customers : orders = 1 : 1), so a person who orders twice has two `customer_id`s and one
`customer_unique_id`. Repeat-customer, retention and cohort metrics must count
`customer_unique_id`; counting `customer_id` makes every customer a one-time buyer. The facts
join this dimension on `customer_unique_id` and keep `customer_id` as a degenerate attribute.
Location = the `customer_id` row of the person's latest purchase (tie, or no order: lowest
`customer_id` in byte order); the location at the time of each order is the facts'
`customer_zip_code_prefix`. `customer_id_count` = number of `customer_id`s (= orders placed,
any status); first/last purchase over orders of any status (NULL if none). Tests: key unique +
not null; zip → `dim_geo`; count > 0; rows = distinct raw `customer_unique_id`.

**`dim_seller`**: `seller_id`, `seller_zip_code_prefix`, `seller_city`, `seller_state` (text).
Tests: key unique + not null; zip → `dim_geo`; rows = raw `sellers`.

**`dim_product`**: `product_id`, `product_category_name`, `product_category_name_english` text,
`has_category_translation` boolean, then the seven integer measures of staging.
`product_category_name_english` is NULL when the product has no category (610 real products)
or its category has no translation (2 real categories: `pc_gamer`,
`portateis_cozinha_e_preparadores_de_alimentos`); no label is invented. Tests: key unique + not
null; `has_category_translation` not null; rows = raw `products`.

**`dim_geo`**: `zip_code_prefix` text, `city`, `state` text, `latitude`, `longitude` double
precision, `geolocation_rows` bigint, `has_geolocation`, `has_invalid_coordinates` boolean. One
row per prefix found in geolocation, customers or sellers, with a deterministic rule (ADR-018):
- `latitude` / `longitude` = **median** (`percentile_cont(0.5)`) of the prefix's geolocation
  points, per axis (robust to the few raw points far outside Brazil);
- if that median lies **outside Brazil's bounding box** (latitude −34..6, longitude −74..−28,
  bounds included; the eastern bound keeps the oceanic islands such as Fernando de Noronha,
  prefix 53990, longitude ≈ −32.4), most of the prefix's points are wrong and the median cannot
  fix them: `latitude` / `longitude` are **NULL** and `has_invalid_coordinates` is true (real
  data: at most 8 prefixes; city / state are kept). `has_invalid_coordinates` is false everywhere
  else, including prefixes without geolocation. A map or distance metric uses rows with
  non-NULL coordinates; `has_geolocation and latitude is null` = coordinates discarded;
- `city` / `state` = the **most frequent (city, state) pair** among the prefix's geolocation rows;
  ties → lowest `state`, then lowest `city`, in byte order (`COLLATE "C"`, independent of the
  database collation). City spellings are kept as written (accent variants are different values);
- a prefix with **no geolocation row** takes the most frequent pair among the customers' and
  sellers' rows with that prefix (same tie rule), with NULL coordinates and
  `has_geolocation = false`.
Tests: key unique + not null + 5-digit format; state in the 27 UF codes; city not null;
covers exactly the source prefixes (singular `assert_dim_geo_covers_every_zip_prefix`);
`has_invalid_coordinates` not null; every non-NULL point inside the bounding box, latitude and
longitude NULL together, flagged rows have geolocation and NULL coordinates, unflagged rows have
coordinates exactly when they have geolocation (singular
`assert_dim_geo_coordinates_inside_brazil`, an error since review round 1 of 2.1; it was a
warning that fired permanently on the real data).

**`fact_order_items`**: `order_id`, `order_item_id` integer, `product_id`, `seller_id`,
`customer_unique_id`, `customer_id`, `customer_zip_code_prefix`, `order_status` (text),
`order_purchased_at` timestamp, `purchase_date` date, `shipping_limit_at` timestamp,
`price_brl`, `freight_value_brl`, `item_total_brl` (= price + freight) numeric, `fx_rate_date`
date, `brl_per_eur`, `price_eur`, `freight_value_eur`, `item_total_eur` numeric.
Every item of every order status is kept (filter by `order_status` downstream).
**FX (ADR-018):** `brl_per_eur` = the latest ECB EUR→BRL rate (`stg_enrichment__fx_rates`,
quote `BRL`) with `rate_date` on or before `purchase_date`, looking back **at most 7 calendar
days** (ECB gaps are weekends and TARGET holidays, at most 4 days); `fx_rate_date` is that
`rate_date`. EUR amount = BRL amount / `brl_per_eur`, rounded to 4 decimals. The rate,
`fx_rate_date` and the EUR columns are NULL together **before the first rate, or when no rate
exists on or within 7 days before the purchase date** (so the last loaded rate is carried up to
7 days past the end of the range, not NULLed at its end). Caveats: one rate per purchase day (not the payment or delivery day); ECB reference
rates, not the rate a customer paid. Tests: key unique; keys not null; `product_id` →
`dim_product`, `seller_id` → `dim_seller`, `customer_unique_id` → `dim_customer`,
`customer_zip_code_prefix` → `dim_geo`, `purchase_date` → `dim_date`, `order_id` →
`fact_deliveries`; status accepted values (as staging); price > 0, freight ≥ 0; **rows = raw
`order_items`**; EUR never NULL for a purchase date inside [first, last] BRL `rate_date`; the
rate is the latest in the look-back (both singular).

**`fact_deliveries`**: `order_id`, `customer_id`, `customer_unique_id`,
`customer_zip_code_prefix`, `order_status` (text); `purchased_at`, `approved_at`,
`delivered_carrier_at`, `delivered_customer_at`, `estimated_delivery_at` timestamp; their dates
`purchase_date`, `approved_date`, `delivered_carrier_date`, `delivered_customer_date`,
`estimated_delivery_date` (date, FK → `dim_date`); `is_delivered` boolean, `delivery_days`,
`delay_days` integer, `is_on_time` boolean. One row per order **whatever its status**; the
timestamps are as in raw (a canceled order may carry a delivery timestamp). Delivery metrics,
**only where `is_delivered`**, else NULL:

| column | definition |
|--------|------------|
| `is_delivered` | `order_status = 'delivered'` and `delivered_customer_at` is not null |
| `delivery_days` | `delivered_customer_date - purchase_date` (whole calendar days) |
| `delay_days` | `delivered_customer_date - estimated_delivery_date` (negative = early, 0 = on the estimated day) |
| `is_on_time` | `delivered_customer_date <= estimated_delivery_date` |

Dates, not timestamps: Olist's estimate is a date (stored at 00:00), so comparing timestamps
would make every delivery on the estimated day late. Tests: `order_id` and `customer_id` unique
+ not null; `customer_unique_id` → `dim_customer`, zip → `dim_geo`, the five dates →
`dim_date`; status accepted values; `is_delivered` not null; **rows = raw `orders`**; metrics
follow the table above and are NULL for non-delivered orders (singular); `delivery_days` ≥ 0
(warning only).

**Unit tests** (`warehouse/models/core/_core__unit_tests.yml`, run by `dbt build`): exact
outputs for the `dim_geo` rule, the FX carry-forward and look-back, the delivery metrics and
`dim_customer`'s grain and location. **Generic tests** in `warehouse/tests/generic/`:
`unique_combination_of_columns`, `positive`, `not_negative`, `matches_regex`, `equal_row_count`
(row-count reconciliation, optionally against a distinct count). Singular tests in
`warehouse/tests/core/`.

---

## 4. Metric definitions — `draft`

Each metric served by the API is defined here (name, formula, grain, filters) in task 2.3.
