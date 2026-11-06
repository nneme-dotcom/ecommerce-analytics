# Progress

Current state at the top; handoff log below (newest first).

## Current state

- **Phase:** 2 — Model and metrics (Phase 1 approved by the owner on 2026-11-04; 1.3 still counting days)
- **Next up:** review of 2.2 (ML price-history model, branch `warehouse/ml-price-model`, `in review`), then 2.3 marts. 2.6 closes once `pg_database_size` is read from a run's job summary (first chained `daily` run 42035192719 succeeded and loaded only `raw_mercadolibre`). 1.3 is at 2 of 3 *scheduled* successes (2026-11-04 and 2026-11-05, both ~8 h late). Neon PostgreSQL version still to be confirmed by the owner. Follow-ups 1.6–1.12 alongside.
- **Blockers:** none.

## Handoff template

```
### YYYY-MM-DD — <agent role> — <task #>
Done:
Not done / open:
Gotchas for the next agent:
Files touched:
```

## Log

### 2026-11-06 — analytics-engineer — 2.2 (branch `warehouse/ml-price-model`)
Done: contract first (DATA_CONTRACT §3: `mercadolibre` source, "Mercado Libre staging", "core
models — Mercado Libre price history", `dim_date` extended). **Source** `mercadolibre`
(`raw_mercadolibre`: runs, highlights, products, offers, trends, `_load_audit`, every column
and `format_type()` type, covered by `assert_sources_match_contract`; source tests = §1b keys
and required columns). **Staging** (views, one row per raw record, every status):
`stg_mercadolibre__runs` / `__highlights` / `__products` / `__offers` / `__trends`; §1b names,
`nullif(text, '')`, `payload` dropped; offers key (`snapshot_date`, `product_id`, `item_id`)
unique; NULL `item_id` and unknown highlight `type` are warnings. **Core** (tables, ADR-019):
`fact_offer_snapshot` (one row per offer per included day; `success` + `partial` days, `failed`
left out, `run_status` on every row; price/original price in `currency_id`, never converted
(no ECB ARS rate, ADR-012); `has_discount` / `discount_amount` / `discount_pct` only where
`original_price > price`; official store, free shipping, logistic type, fulfillment, deal;
best-seller category/position = the product's lowest position that day across categories
(ties by byte order) + `best_seller_category_count`; `offers_in_product`,
`price_rank_in_product` per currency), `dim_ml_product`, `dim_ml_seller`, `dim_ml_category`
(type 1, latest included value; members from every place an id appears). `dim_date` is now
conformed: whole years of the Olist timestamps **and** the ML snapshot dates (real 2016–2026,
4,018 rows) plus `is_ar_public_holiday` / `ar_holiday_name` (BR rule). Macro
`ml_included_run_statuses`; generic test `expression_is_true`; singular tests
`assert_fact_offer_snapshot_reconciles_with_raw` (per day, from the source),
`_discount_rules`, `_best_seller_matches_highlights` (re-derived with `distinct on`, plus
offer count / rank range), `assert_ml_dims_cover_their_sources`; the holiday-flag test now
checks BR and AR. 3 unit tests (`_core__ml_unit_tests.yml`). **Fixtures:**
`warehouse/ci/ml_fixture_days.py` writes 3 days (success / partial / failed) with the real
snapshot writer from `tests/mercadolibre/ml_fixtures.py`; `fixture_db load` loads them with
`ingestion.mercadolibre.load` (`full_reload`), counts them in the refusal guard and the
inventory; cases in `warehouse/fixtures/README.md`. ADR-019. PLAN 2.2 → `in review`.
Validation on PostgreSQL 16.15 (initdb as non-root `claude` under /tmp, port 40549, deleted
afterwards; no Neon): `ruff check .` clean; fresh DB `fixture_db load` → `dbt build`
**PASS=364** (1 hook, 27 models, 7 unit tests, 329 data tests; WARN=0 ERROR=0) →
`fixture_db check` ok (29 raw relations unchanged); `pytest tests/warehouse` 42 passed (5 new:
ML refusal, ML loaded through the loader with its edge cases, reproducible day folders, 2 new
`check` cases); full `pytest` 404 passed, 2 skipped (Olist real data). Fixture core rows: fact 11, products 5, sellers 6,
categories 3, `dim_date` 3,652 (2017–2026). Negative checks: 6 model mutations (`>=` discount,
failed days kept, reversed tie order, NULL `item_id` kept, earliest seller state, failed-day
product rows) each fail their unit test; with unit tests excluded the data tests still catch the
discount and tie mutations (the other four cannot occur in loader-produced data); 9 corruptions
of the built tables (deleted fact row, wrong pct, discount on an equal price, wrong position,
deleted seller, extra category, row on the failed day, rank out of range, unflagged AR holiday)
each fail the intended test.
**Real-data check** (data repo cloned to the scratchpad, 4 days 2026-11-03..06, all `success`;
throwaway DB; clone and DB deleted; counts only): `raw_mercadolibre` loaded by the real loader
(offers 9,439); real `raw_olist` from `olist/archive.zip`; enrichment **fixtures** (Frankfurter /
Nager blocked by the proxy here, so Olist EUR and most holiday flags are NULL in that run).
`dbt build` PASS=364 WARN=0 ERROR=0. `fact_offer_snapshot` 9,439 (= raw offers; 2,313 / 2,306
/ 2,466 / 2,354 per day), `dim_ml_product` 112, `dim_ml_seller` 817, `dim_ml_category` 19 (5
ranking, 18 listing), `dim_date` 4,018; 4,219 offers with a discount, 0 products in two
categories on one day, 0 products without details; fact ≈ 2.0 MB (≈ 220 B a row).
Not done / open: never run on Neon (the next `daily` run after merge builds it; expect PASS=364,
WARN=0 and the counts above plus each new day). Category names are not captured (no
`/categories/{id}` call): an ingestion task if the dashboard needs them. Trends have a staging
view but no core model.
Gotchas for the next agent: **ML core tables are offer-level and never exposed by the API**
(ADR-003); 2.3 marts must aggregate. Count-based ML metrics must decide about `partial` days
(`run_status`). Never sum prices across `currency_id`, and ARS cannot be compared with Olist's
BRL/EUR. `best_seller_position` is a demand proxy, comparable within one category and day. The
offer list is capped at 100 per product (7 of 337 real product-days), so `offers_in_product` /
`price_rank_in_product` cover the captured offers only. `dim_date` now reads
`stg_mercadolibre__runs`: a database without `raw_mercadolibre` cannot build. dbt unit-test
inputs with `text[]` columns must be `format: sql` (dbt-postgres types dict fixtures as `ARRAY`).
`fixture_db` imports `tests/mercadolibre/ml_fixtures.py` by path (tests/ is not a package): keep
that file's helper signatures stable or update `ml_fixture_days.py`. The fact is rebuilt in full
daily (≈ 190 MB a year): make it incremental by `snapshot_date` before Neon's 1 GB matters.
Files touched: warehouse/models/staging/mercadolibre/** (new), warehouse/models/core/
{fact_offer_snapshot,dim_ml_product,dim_ml_seller,dim_ml_category}.sql,
`_core__ml_models.yml`, `_core__ml_unit_tests.yml` (new), warehouse/models/core/dim_date.sql,
`_core__models.yml` (dim_date description), warehouse/macros/ml_included_run_statuses.sql (new),
warehouse/tests/generic/expression_is_true.sql (new), warehouse/tests/core/
{assert_fact_offer_snapshot_reconciles_with_raw,assert_fact_offer_snapshot_discount_rules,
assert_fact_offer_snapshot_best_seller_matches_highlights,assert_ml_dims_cover_their_sources}.sql
(new), warehouse/tests/core/assert_dim_date_holiday_flag_matches_calendar.sql,
warehouse/ci/{fixture_db.py,ml_fixture_days.py (new)}, warehouse/fixtures/README.md,
warehouse/README.md, tests/warehouse/test_fixture_db.py, docs/DATA_CONTRACT.md (§3),
docs/DECISIONS.md (ADR-019), docs/PLAN.md, docs/PROGRESS.md

### 2026-11-05 — orchestrator — 2.1 live on Neon
Done: PR #15 merged (67c1073) after two review rounds, both with a dbt build on the real Olist data
in a throwaway database (PASS=222 WARN=0, 7 prefixes with `has_invalid_coordinates`). Manual
`mode=daily` run 42112919312 on Neon: ML 0 days loaded / 3 unchanged, `dbt build` PASS=222 WARN=0
ERROR=0 on the real data. PLAN 2.1 → done.
The first chained `daily` run (42035192719, after scheduled snapshot 42034833284 on 2026-11-05 at
13:20 UTC) succeeded and loaded only `raw_mercadolibre` (Olist/enrichment skipped). 1.3: 2 of 3
scheduled successes.
Not done / open: 2.6 needs `pg_database_size` read from a job summary (owner, Actions UI).
Files touched: docs/PLAN.md, docs/PROGRESS.md

### 2026-11-05 — analytics-engineer — 2.1 (branch `warehouse/olist-star`)
Done: contract first (DATA_CONTRACT §3 "staging models" and "core models": grain, columns, types,
keys, tests, expected real row counts). **staging** (views, one row per raw record): the eight
missing Olist models (`customers`, `geolocation`, `order_items`, `order_payments`,
`order_reviews`, `orders`, `products`, `sellers`) + `stg_enrichment__fx_rates` /
`__public_holidays`; `nullif(col, '')` everywhere, casts, `*_lenght` → `*_length`, zip prefixes
padded to 5 digits when shorter (macro `zip_prefix`, never truncates) and tested against
`^[0-9]{5}$`. Reviews grain = (`review_id`, `order_id`). **core** (tables): `dim_date` (whole
years spanning the five order timestamps; `is_br_public_holiday` = BR, `is_global`, type `Public`;
NULL in a year with no BR calendar), `dim_customer` (grain `customer_unique_id`; latest-purchase
location; `customer_id` vs `customer_unique_id` documented), `dim_seller`, `dim_product` (English
category, `has_category_translation`), `dim_geo` (one row per prefix from geolocation ∪ customers ∪
sellers; per-axis median lat/lng; most frequent (city, state) pair, ties byte order; fallback
for prefixes without geolocation), `fact_order_items` (BRL + EUR at the latest ECB EUR→BRL rate on
or before the purchase date, 7-day look-back, `fx_rate_date`), `fact_deliveries` (one row per
order, all statuses; `is_delivered`, `delivery_days`, `delay_days`, `is_on_time` by calendar date,
NULL unless delivered). Natural keys everywhere. ADR-018 (grains, keys, geo rule, FX rule).
Tests: generic `not_negative`, `matches_regex`, `equal_row_count` (row-count reconciliation:
items = raw `order_items`, deliveries = raw `orders`, sellers, products, customers = distinct
`customer_unique_id`); 7 singular tests in `warehouse/tests/core/` (dim_date contiguity and
holiday rule, dim_geo coverage, FX not NULL inside the rate range, FX = latest rate in the
look-back, delivery-metric rules; Brazil bounding box as a warning); 4 dbt unit tests (geo rule,
FX carry-forward/look-back, delivery metrics, dim_customer). Fixtures extended (10 orders, 10
customer_ids for 8 people, 3 sellers, 5 products, 8 geolocation rows; every case listed in
`warehouse/fixtures/README.md`); FX/holiday fixtures unchanged (unverifiable here, PLAN 1.11).
Validation on PostgreSQL 16.15 (initdb as non-root `claude` under /tmp, port 46853, deleted
afterwards): `ruff check .` clean; `fixture_db load` → `dbt build` **PASS=221** (1 hook, 11 views,
7 tables, 4 unit tests, 198 data tests; WARN=0 ERROR=0) → `fixture_db check` ok (schemas
`core`, `staging`; 17 raw relations unchanged); `pytest tests/warehouse` 37 passed; full `pytest`
399 passed, 2 skipped (Olist real data). Negative check: nine corruptions of the built core tables
(wrong `fx_rate_date`, a NULL rate inside the range, a deleted item, wrong `delivery_days`, a
metric on an undelivered order, a deleted `dim_geo` prefix, an unflagged holiday, a missing day,
a deleted customer) each failed the intended test (10 failures). No Neon connection.
Expected real row counts: `dim_date` 1,096; `dim_customer` 96,096; `dim_seller` 3,095;
`dim_product` 32,951; `dim_geo` ≈ 19.2k; `fact_order_items` 112,650; `fact_deliveries` 99,441;
`core` ≈ 80 MB in total.
Not done / open: **the real-data check happens after merge**: `gh workflow run warehouse-load.yml
--ref main -f mode=daily` (runs `dbt build` on Neon); PLAN 2.1 stays `in review` until it is
green. Assumptions about the real data that only that run proves (from the public dataset's
known properties, not checked here): (`review_id`, `order_id`) unique; (`order_id`,
`payment_sequential`) unique; `customer_id` unique in orders; every `*_state` a UF code; zip
prefixes 5 digits; no empty city; order statuses and payment types as listed; all ids
referentially complete. If one fails, the failing test names the rule; fix the test or the
contract, not the data.
Gotchas for the next agent: every `dbt build` (also each `daily` run) rebuilds all `core` tables
although Olist is static (ADR-018 consequences; a later task can select only ML models on daily
runs). dbt unit tests compare numerics as text, so expected EUR values carry the 4-decimal scale
(`'2.5000'`). Plain YAML descriptions containing `: ` must be quoted (dbt parse error). Count
people with `customer_unique_id`, never `customer_id`. Payments and reviews have staging models
but no core fact yet (2.3).
Files touched: warehouse/models/staging/olist/*.sql + `_olist__models.yml`,
warehouse/models/staging/enrichment/{stg_enrichment__fx_rates,stg_enrichment__public_holidays}.sql
+ `_enrichment__models.yml` (new), warehouse/models/core/** (new), warehouse/macros/zip_prefix.sql
(new), warehouse/tests/generic/{not_negative,matches_regex,equal_row_count}.sql (new),
warehouse/tests/core/*.sql (new), warehouse/fixtures/raw_olist/*.csv (8 of 9),
warehouse/fixtures/README.md, warehouse/README.md, warehouse/dbt_project.yml,
tests/warehouse/test_fixture_db.py (orders count read from the fixture plan),
docs/DATA_CONTRACT.md (§3), docs/DECISIONS.md (ADR-018), docs/PLAN.md, docs/PROGRESS.md

**Review round 1 (2026-11-05, PR #15 approved with three findings, fixed in 2e528c1; round 2 APPROVE with a real-data re-run: PASS=222 WARN=0, 7 prefixes flagged).**
Real-data results the reviewer measured before the fix (counts only): `dim_date` 1,096,
`dim_customer` 96,096, `dim_seller` 3,095, `dim_product` 32,951, `fact_order_items` 112,650,
`fact_deliveries` 99,441 (as expected); `dbt build` **PASS=220 WARN=1** (the geo bounding-box
warning, 8 prefixes); `core` ≈ 70 MB; whole database ≈ 253 MB. Every real-data assumption above held.
Fixes: (1) **FX test cost**: `assert_fact_order_items_fx_rate_is_latest_on_or_before_purchase`
computes the expected rate date once per distinct `purchase_date` and joins that to the items
on equality (was a 112,650 items × all rates nested loop, 10.5 s on real data, the slowest
step of every daily build). Synthetic real-size check locally (112,650 items, 630 days, 565 BRL
rates): ≈ 2.6 s → ≈ 75 ms, and the old and new queries flag the same 100 rows with the same
reasons after 3 mutation types; on the fixtures, seven mutations (wrong `fx_rate_date` with the
rate kept, an older published date with its rate, a date after the purchase, a dropped rate
inside the look-back, a rate past day 7, a rate before the first rate, a wrong `brl_per_eur`)
each fail it. (2) **Geo warning**: option (a). `dim_geo` gets `has_invalid_coordinates`;
a prefix whose median lies outside Brazil's bounding box (macro `inside_brazil_bbox`, lat −34..6,
lng −74..−28: the eastern bound moved from −34 to −28 so Fernando de Noronha, prefix 53990, is
not NULLed) has NULL `latitude` / `longitude`. `assert_dim_geo_coordinates_inside_brazil` is now
an **error** that checks the published result (six corruptions each fail it); fixture prefix
45000 (2 of 3 points in Lisbon) and the unit test (plus a Noronha point that is kept) exercise it.
Contract §3 and ADR-018 updated. (3) **FX wording** in contract §3, ADR-018 and the
`fact_order_items` header: NULL before the first rate, or when no rate exists on or within 7 days
before the purchase date.
Validation on PostgreSQL 16.15 (initdb as non-root `claude` under /tmp, port 52649, deleted
afterwards): `ruff check .` clean; `fixture_db load` → `dbt build` **PASS=222 WARN=0 ERROR=0**
(199 data tests: + `has_invalid_coordinates` not null) → `fixture_db check` ok; `pytest
tests/warehouse` 37 passed; full `pytest` 399 passed, 2 skipped. Expected on real data:
PASS=222 WARN=0, with 7 prefixes flagged (measured in review round 2; the 8th, near Fernando de Noronha, is kept).
**Facts for 2.3 (from the reviewer, real data), not errors in 2.1:** 35 sellers' `seller_state`
differs from their prefix's `dim_geo.state`; 166 orders have `delivered_carrier_at` before
`purchased_at`; 775 orders have no items and 1 has no payment; 174 zip prefixes have a tied
(city, state) pair (resolved by the byte-order rule). Marts must pick which state they report
(seller's own vs `dim_geo`), exclude or flag negative carrier lead times, and not assume every
order has items and a payment (use `fact_deliveries` for order counts).
Files touched (round 1): warehouse/models/core/{dim_geo.sql,fact_order_items.sql,
_core__models.yml,_core__unit_tests.yml}, warehouse/macros/inside_brazil_bbox.sql (new),
warehouse/tests/core/{assert_dim_geo_coordinates_inside_brazil,
assert_fact_order_items_fx_rate_is_latest_on_or_before_purchase}.sql,
warehouse/fixtures/raw_olist/olist_geolocation_dataset.csv, warehouse/fixtures/README.md,
docs/DATA_CONTRACT.md (§3), docs/DECISIONS.md (ADR-018), docs/PROGRESS.md

### 2026-11-04 — orchestrator — 2.6 first live run
Done: PR #13 merged (b401a7e). Owner confirmed (in chat; the environments API is not readable from
the workspace) that the Environment `warehouse` allows only the `main` branch. First `mode=full` run
41929479775 on Neon (region EU Frankfurt, per the owner's Neon console): every step green.
`raw_mercadolibre`: 2 days (2026-11-03, 2026-11-04): highlights 200, products 172, offers 4,619,
trends 100. `raw_olist`: the nine tables match DATA_CONTRACT §2 counts (orders 99,441; geolocation
1,000,163; …). `raw_enrichment`: fx_rates 5,210, public_holidays 74. `dbt build` PASS=38 on the real
data. Log check: no unmasked host, endpoint id or password in the run log.
The scheduled ML snapshot for 2026-11-04 did fire, at 13:06 UTC (run 41835070250, `success`),
about 8 h after the 05:23 cron: GitHub delayed it, it did not skip (this corrects the earlier
entries below that say it did not fire). 1.3: 1 of 3 scheduled successes (the 2026-11-03 success
was a manual `workflow_dispatch`).
Not done / open: 2.6 stays `in review` until (a) the first chained `daily` run (after the next
scheduled snapshot) succeeds and loads only `raw_mercadolibre`, and (b) `pg_database_size` is read
from a run's job summary (Actions UI; the API does not return it). Verified live so far: main-only
run in the Environment, data repo fetch, URL split and masking, all three raw loads, dbt build on
real data. Neon PostgreSQL version not yet confirmed. 1.3 needs two more scheduled successes.
Gotchas for the next agent: the warehouse now holds real data; develop models against a local
throwaway DB with fixtures, and validate on Neon only through `warehouse-load.yml` on `main`.
Files touched: docs/PLAN.md, docs/PROGRESS.md

### 2026-11-04 — devops — 2.6 (branch `devops/warehouse-load`)
Done: `.github/workflows/warehouse-load.yml` ("Warehouse load"): `workflow_run` after each
successful *ML daily snapshot* of main (mode `daily`) + `workflow_dispatch` (`mode: daily | full`).
Job: `if` main only (and, for workflow_run, conclusion success / head branch main / same repo),
`environment: warehouse`, `permissions: contents: read`, job-level concurrency
`warehouse-load-<ref>` without cancel-in-progress, job timeout 60 min and a timeout on every step,
`persist-credentials: false` on the (only) `actions/checkout`. The data repo is fetched by `git init`
+ shallow `git fetch` with `DATA_REPO_PAT` as an env-only `http.extraheader` (never URL, argv or
`.git/config`; ADR-009 push pattern). Steps: split URL → load `raw_mercadolibre` → plan → (Olist) →
(FX, holidays) → `dbt build` → summary. `daily` loads `raw_olist` only when a CSV in
`olist/archive.zip` differs from the last `raw_olist._load_audit` hash (or never loaded), and
`raw_enrichment` only when never loaded; `full` loads all three. New `scripts/warehouse_env.py`
(stdlib): parses `WAREHOUSE_DATABASE_URL`, prints `::add-mask::` for user, password (encoded and
decoded; `%` values escaped and raw), host (as written and lower-cased), the endpoint id and dbname,
then writes `DBT_PG_*`, `DBT_ENV_SECRET_PG_PASSWORD`, `DBT_PG_CHANNEL_BINDING` to `$GITHUB_ENV`
(heredocs); refuses missing parts, control chars, multi-host, sslmode other than `require`,
unknown query params; never prints a value, never uses outputs. New `scripts/warehouse_report.py`
(`plan`, `summary`; run as `python -m scripts.warehouse_report`, it imports `ingestion`): summary =
step outcomes, row counts per raw table, ML days, dbt counts by type/status + failing node ids,
`pg_database_size` + per-schema size, Olist `archive_sha256`; warning at 80 % of 1 GB.
channel_binding: dbt-postgres 1.11 has no profile field; psycopg2-binary 2.9.13 bundles libpq 17,
which reads `PGCHANNELBINDING`; exported in the dbt step only, always a valid keyword (empty is
fatal in libpq). ADR-017; DATA_CONTRACT §1 (data repo top level), §2 (`olist/archive.zip`), §3
(warehouse on Neon: schemas, loaders, daily vs full). PLAN 2.6 → `in review`.
Validation (no Neon, no secrets): actionlint 1.7.12 (release tarball, sha256 verified against the
release checksums) on all five workflows: 0 errors; shellcheck 0.11.0 (PyPI `shellcheck-py` wheel,
`--require-hashes`) on every extracted `run:` block: clean at default severity (with
`--enable=all`: only style hints and env-provided variables). Local end-to-end on PostgreSQL 16
with TLS (self-signed) + a SCRAM role, URL with a percent-encoded `p@ss:...%...` password,
`sslmode=require&channel_binding=require`: split → `$GITHUB_ENV` parsed like the runner → fixture
load through the loaders with that URL → `dbt build` PASS=38 with `PGCHANNELBINDING=require` (only
possible with SCRAM channel binding over TLS) and `prefer`; `dbt debug` fails with an empty value
("invalid channel_binding value") and with `require` without TLS ("channel binding required, but
SSL not in use"), proving dbt's libpq reads it; summary rendered with no user/host/dbname/password.
That dbt error message quoted the host, so host masking is load-bearing. Tests: 45 in
`tests/test_warehouse_env.py`, 21 in `tests/test_warehouse_report.py` (plan decisions on fixture
loads: empty, unchanged, changed CSV, unreadable/missing archive, dropped tables, partial audit;
CLI outputs booleans only; DB errors print the type only). `ruff check .` clean; full `pytest`
(with dbt installed): 374 passed, 2 skipped (Olist real data). Throwaway cluster (non-root user
`claude`, /tmp, free port) deleted.
Not done / open: never run on GitHub (needs the Environment secret). Acceptance "first full run
builds green on the real data" is checked after merge: `gh workflow run warehouse-load.yml --ref
main -f mode=full`. Risks for that run: (1) the URL may carry a query parameter besides
`sslmode`/`channel_binding` (the split step then fails naming the key; drop it from the secret or
allow it in the script); (2) `olist/archive.zip` stored via LFS fails with a clear error;
(3) Neon cold start: loaders have no connect timeout (bounded by the step timeout), dbt has 10 s
and 1 retry; (4) loader and dbt error messages are printed by those tools: the masks cover the URL
parts, but a COPY error from the Olist loader can still quote a (public) Olist value, and a dbt
SQL error could quote a raw value; (5) a pending `full` dispatch can be displaced by a chained
`daily` run (re-dispatch); (6) masking a short dbname (e.g. `neondb`) also stars it inside other
words in logs (cosmetic).
Proposed CLAUDE.md Commands rows (orchestrator):
- `| Load the Neon warehouse and dbt build (main only, Environment warehouse; chained after each successful ML snapshot as mode daily) | gh workflow run warehouse-load.yml --ref main -f mode=full (or -f mode=daily; watch: gh run watch) |`
- `| Split a warehouse URL into the dbt profile variables, masked (GitHub Actions only; env WAREHOUSE_DATABASE_URL, GITHUB_ENV) | python scripts/warehouse_env.py |`
- `| Decide the optional raw loads / write the warehouse job summary (env DATABASE_URL) | python -m scripts.warehouse_report plan --mode daily\|full --olist-zip <data-repo>/olist/archive.zip · python -m scripts.warehouse_report summary --mode <m> [--run-results warehouse/target/run_results.json] [--olist-manifest <dir>/manifest.json] |`
and the exit-code line "warehouse_env.py exits 0 ok, 2 URL missing/malformed/not allowed;
warehouse_report exits 0 ok, 1 database/input error, 2 configuration error".
Gotchas for the next agent: the `workflow_run` filter matches the snapshot workflow's `name:`
("ML daily snapshot"); renaming it silently stops the chain. `scripts.warehouse_report` must be run
with `-m` from the repo root (namespace package, imports `ingestion`). A new raw schema needs a
row in the summary's `RAW_SCHEMAS` and a load step. `DBT_PG_CHANNEL_BINDING` is not a profile
variable: only the dbt step maps it to `PGCHANNELBINDING`. 2.1 can use the first full run as its
"full real load" (PLAN 2.1).
Files touched: .github/workflows/warehouse-load.yml (new), scripts/warehouse_env.py (new),
scripts/warehouse_report.py (new), tests/test_warehouse_env.py (new),
tests/test_warehouse_report.py (new), docs/DATA_CONTRACT.md (§1, §2, §3), docs/DECISIONS.md
(ADR-017), docs/PLAN.md, docs/PROGRESS.md

Review round 1 (PR #13 approved with low findings; fixed in 924c72c, round 2 APPROVE): L1
`warehouse_env.py` now refuses, before `urlsplit`, whitespace or any control character (Unicode
`Cc`, so C1 too) in the URL after the same `.strip()` as the loaders, and refuses a netloc with
more than one `@` (Python splits user info at the last `@`, libpq at the first; it now splits at
the only one). Messages name the problem only. Nit 1: port 0 refused; `5432 if port is None else
port`. Nit 2: every `::error` in both scripts goes through `escape_command_data` (new
`warehouse_env.error_command`, reused by `warehouse_report`); tests with a query key holding a
decoded newline, CRLF and a literal `%0A`. L3: `plan` also loads `raw_enrichment` on a daily run
when `public_holidays` has no `AR` row for the current UTC year (one `EXISTS` read, no API call;
`utc_today()` / `decide(today=...)` injectable; test proves the plan works on a read-only
session). L4: plan and the Olist/FX/holidays steps run when the ML load failed (`!cancelled()` and
`steps.ml.outcome` is `success` or `failure`, so a failed setup step still stops everything) and
depend only on the plan; `dbt build` runs only if ML and plan succeeded and Olist/FX/holidays are
`success` or `skipped`; any failed step keeps the job red. L2: ADR-017 sentence on the archive
sha256 (log + summary) vs the durable per-CSV `file_sha256`. Nit 4: DATA_CONTRACT §2 says a CSV
hash change triggers the reload and a repack with identical CSVs does not. Nit 3 left as is
(accepted). ADR-017 (modes, archive, URL split, failure handling, consequences) and DATA_CONTRACT
§2/§3 updated. PLAN 2.6 stays `in review` until the first live `mode=full` run.
Validation: `ruff check .` clean; full `pytest` with `requirements-dbt.txt` installed (dbt-core 1.12.6,
dbt-postgres 1.11.0) against a real PostgreSQL 16 (initdb as non-root `claude` under /tmp, free port,
deleted afterwards): 399 passed, 2 skipped (Olist real data); `tests/test_warehouse_env.py` 65
(+20), `tests/test_warehouse_report.py` 26 (+5). actionlint 1.7.12 (release tarball, sha256
verified against the release checksums file) clean on all workflows; shellcheck not available in
this session, so the changed `run:` blocks were not re-shellchecked (only `if:` conditions and
comments changed in the workflow, no shell).
Gotcha: if the January holidays load fails (Nager.Date down), every daily run retries it and stays
red until it succeeds; that is intended (loud), not a loop to silence.
Files touched (round 1): .github/workflows/warehouse-load.yml, scripts/warehouse_env.py,
scripts/warehouse_report.py, tests/test_warehouse_env.py, tests/test_warehouse_report.py,
docs/DECISIONS.md (ADR-017), docs/DATA_CONTRACT.md (§2, §3), docs/PROGRESS.md

### 2026-11-04 — analytics-engineer — 2.0 (branch `warehouse/dbt-skeleton`)
Done: dbt project in `warehouse/`: `dbt_project.yml` (`staging` views; `core`/`marts` blocks
commented until their first model, otherwise dbt warns about unused paths), `profiles.yml` (env only:
`DBT_PG_HOST/PORT/USER/DBNAME/SSLMODE`, `DBT_ENV_SECRET_PG_PASSWORD`; target schema `staging`),
`generate_schema_name` (models/seeds/snapshots must be in `staging`/`core`/`marts`, otherwise a
compile error). Sources `olist` (nine tables + `_load_audit`) and `enrichment` (`fx_rates`,
`public_holidays`, `_load_audit`) with every column and type; source tests only where §2/§2b
guarantee them. Singular test `assert_sources_match_contract` (declared columns/order/types =
database); generic tests `unique_combination_of_columns`, `positive` (no dbt packages). Smoke model
`staging.stg_olist__product_category_translation`. CI job `dbt` (ci.yml): postgres:16 service,
no secrets; `python -m warehouse.ci.fixture_db load` loads `warehouse/fixtures/` with the real
loaders' functions, then `dbt build`, then `fixture_db check` (no schema besides
staging/core/marts; raw relations and row counts unchanged). ADR-016; DATA_CONTRACT §3.
Pins: dbt-core 1.12.6 + dbt-postgres 1.11.0, installed together with requirements.txt in a fresh
venv (Python 3.13), `pip check` clean. Local run on PostgreSQL 16: `dbt build` PASS=37
(1 view, 36 tests), WARN=0, ERROR=0; `check` ok (only `staging` created, 13 raw relations
unchanged). Negative checks: a drifted source column name and type → the contract test returns
exactly those rows; a model with no schema or `schema='raw_olist'` → compile error; an extra schema,
a view in `raw_olist` and deleted fx rows → `check` exit 1 naming all three. `ruff check .` clean;
`pytest`: 207 passed, 2 skipped. actionlint 1.7.12 clean on ci.yml.
Not done / open: the CI job has not run on GitHub yet (first run is this PR). The star schema is 2.1.
Proposed CLAUDE.md Commands rows (orchestrator):
- `Install dbt (warehouse)` | `pip install -r requirements-dbt.txt`
- `Load warehouse CI fixtures into a throwaway DB (env DATABASE_URL, FIXTURE_DB_ALLOW=1; refuses a DB with more rows than the fixtures)` | `FIXTURE_DB_ALLOW=1 python -m warehouse.ci.fixture_db load --inventory <file>`
- `dbt build (env DBT_PG_HOST, DBT_PG_PORT, DBT_PG_USER, DBT_ENV_SECRET_PG_PASSWORD, DBT_PG_DBNAME, DBT_PG_SSLMODE; all optional, local defaults)` | `dbt build --project-dir warehouse --profiles-dir warehouse`
- `Check dbt created only staging/core/marts and left raw_* and public unchanged` | `python -m warehouse.ci.fixture_db check --inventory <file>`
- `Test the dbt guards (needs dbt; temp project copies)` | `pytest tests/warehouse`
Gotchas for the next agent: run dbt from the repo root with `--project-dir warehouse
--profiles-dir warehouse` (`fixture_db` is `python -m warehouse.ci.fixture_db`, a namespace
package; it imports `ingestion`). A new model folder needs a `+schema` in `dbt_project.yml`
(uncomment `core`/`marts`). A new raw table must be declared with **all** its columns and
`format_type()` types, or `assert_sources_match_contract` fails (2.2 adds `raw_mercadolibre` this
way, plus fixtures through the 2.5 loader). Generic test arguments go under `arguments:` (dbt 1.12
style). The Olist fixture load logs "no manifest.json" on purpose (no checksums to maintain).
Staging views on raw tables make a loader's drop-and-re-create on column drift fail (intended, §2).
ADR-015 is taken by task 2.5 (PR #11), so this one is ADR-016.
Files touched: warehouse/** (new), requirements-dbt.txt (new), .github/workflows/ci.yml,
docs/DATA_CONTRACT.md (§3), docs/DECISIONS.md (ADR-016), docs/PLAN.md, docs/PROGRESS.md
Review round 1 (PR #12, same day): (1) snapshot `target_schema` bypass closed:
`generate_schema_name` now rejects `target_schema`/`target_database` on any node and a target
schema outside staging/core/marts (parse-time, so every command fails); new `on-run-start` hook
`assert_node_schemas` re-checks every node's final schema/database before anything runs. Verified
the reviewer's `config(schema='staging', target_schema='raw_olist')` resolved to `raw_olist` with the
old macro and fails parsing now. (2) ci.yml comment ADR-015 → ADR-016. (3) `fixture_db load` refuses
(exit 2, nothing written) without `FIXTURE_DB_ALLOW=1` (set only on the CI load step) and when any
table it writes has more rows than the fixtures (e.g. `raw_olist.orders`). (4) `check` compares a
full inventory of `raw_*` and `public`: per relation kind, column signature (names, order,
`format_type()`), row count, md5 of the rows sorted as text, view/index definitions; plus
constraints, triggers, functions, standalone types. (5) fixtures README: transactional rows
synthetic, reference values (3 category translations, holidays, FX rates) real public facts.
(6) `assert_sources_match_contract`: a column without `data_type` is a failure row. (7) dbt job
checkout `persist-credentials: false`. New tests: `tests/warehouse/test_fixture_db.py` (25: both
refusals, idempotent reload, check catching a same-count UPDATE, ADD COLUMN, type change, function
in `raw_olist`, table/view in `public`, view/index/constraint/type in raw, deleted rows, dropped
table, extra schema, missing staging) and `tests/warehouse/test_dbt_guards.py` (12, need dbt, so
skipped in the python job; the dbt job now installs pytest and runs `pytest -q tests/warehouse`
after `check`): real `dbt parse`/`compile`/`run`/`test` on temp project copies (snapshot
target_schema/target_database in config or dbt_project.yml, raw/missing schemas, bad target schema,
hook stopping a run when the macro is weakened, missing `data_type` → exactly one failure row).
Results on PostgreSQL 16 (local cluster): `ruff check .` clean; `pytest` 244 passed, 2 skipped;
`fixture_db load` → `dbt build` PASS=38 (1 hook, 1 view, 36 tests) → `check` ok (17 relations).
Gotcha: `check` hashes all rows, so it is for fixture DBs only; dbt creates a model's schema
*before* on-run-start, so the hook cannot prevent an empty stray schema (the macro and `check` do).
Files touched (round 1): warehouse/ci/fixture_db.py, warehouse/macros/generate_schema_name.sql,
warehouse/macros/assert_node_schemas.sql (new), warehouse/dbt_project.yml,
warehouse/tests/assert_sources_match_contract.sql, warehouse/README.md, warehouse/fixtures/README.md,
tests/warehouse/ (new), .github/workflows/ci.yml, docs/DATA_CONTRACT.md (§3), docs/DECISIONS.md
(ADR-016), docs/PROGRESS.md

### 2026-11-04 — data-engineer — 2.5 (PR #11, branch `ingestion/ml-raw-load`, after review round 1)
Done: DATA_CONTRACT **§1b `raw_mercadolibre`** (contract first): `runs`, `highlights`, `products`,
`offers`, `trends` (typed columns = exactly the §1 fields + `line_no` + full-line `payload jsonb`)
and an append-only `_load_audit` (one row per table per day loaded). `python -m
ingestion.mercadolibre.load --source <data-repo-root> [--full-reload]` (env `DATABASE_URL`): validates
**every** `snapshot_date=` folder before touching the DB (run.json present/well-formed, gzip JSONL,
meta fields = folder/run.json, required keys, line count = `row_counts`; errors name file:line,
never content), then one transaction: per changed day delete that date from the five tables, `COPY`,
DB count check, audit rows. ADR-015: days whose five file SHA-256s equal `runs.file_sha256` are
skipped (Neon WAL/storage); `--full-reload` or a (re-)created table reloads all. Drift
(`raw_schema.py`): `_load_audit` checked first and never dropped (`SchemaDriftError`, exit 1); the
loader's own data tables are re-created without `CASCADE`. psycopg errors of SQLSTATE class 22/23
are logged as class + SQLSTATE only (COPY errors can quote row values; Actions logs are public).
Tests: `tests/mercadolibre/test_ml_raw_load.py`, 49 tests on day folders written by the real
`snapshot.write_snapshot` from `ml_fixtures` payloads (idempotency/skip, full reload, re-run day,
count mismatch, missing run.json/file, malformed/blank/non-object lines, bad gzip, meta mismatch,
NaN/NUL, lenient typing, failed day, orphan DB day kept, data-table drift, audit drift, dependent
view). `ruff check .` clean; full `pytest`: 256 passed, 2 skipped (Olist real data), PostgreSQL 16.
Real-data check: cloned the data repo to the scratchpad (outside the worktree), loaded 2026-11-03
into a throwaway DB: exit 0; runs 1, highlights 100 (PRODUCT 87, USER_PRODUCT 10, ITEM 3), products
87, offers 2,313, trends 50 (= run.json); 0 NULL price/item_id/seller_id; 0 typed-vs-payload
mismatches; 0 duplicate (date, product, item). Re-run: 0 loaded, 1 unchanged; `--full-reload`
identical counts. Clone and DB deleted; no real data copied anywhere.
Not done / open: never run against Neon (2.6). Proposed CLAUDE.md Commands row (orchestrator):
`| Load ML snapshots from the data repo into raw_mercadolibre (env DATABASE_URL; skips unchanged days, ADR-015) | python -m ingestion.mercadolibre.load --source <data-repo-root> [--full-reload] |`
and the exit-code line "ML raw loader exits 0 ok, 1 validation/load failed (nothing changed), 2
configuration error" (same convention as Olist/enrichment).
Gotchas for the next agent: 2.6 passes the data repo checkout root (the folder holding
`mercadolibre/`), not `mercadolibre/` itself. Daily runs normally write one day; a re-run day is
picked up via its new hashes. A dbt view on a raw ML table blocks a drift re-create (exit 1, by
design). `offers.item_id` is nullable and not a key in raw: test uniqueness of (snapshot_date,
product_id, item_id) in staging (2.2). `runs.status` includes `failed` days (empty files); filter
downstream. The drift check ignores constraints (as `store.py`, PLAN 1.8). `store.py` was not
changed; `raw_schema.py` already checks `_load_audit` first (the 1.8 finding).
Files touched: ingestion/mercadolibre/load.py, ingestion/mercadolibre/raw_schema.py (new),
tests/mercadolibre/test_ml_raw_load.py (new), docs/DATA_CONTRACT.md (§1b), docs/DECISIONS.md
(ADR-015), docs/PLAN.md, docs/PROGRESS.md
Review round 1 (fixes in 756fc79; round 2 APPROVE): (1, blocking) orphan days are now read
from the union of all five data tables **before** any drop (`raw_schema` split into
`prepare_schema` / `stale_data_tables` / `recreate_data_tables`); a (re-)create while orphans exist
raises `LoadError` (rolled back, exit 1, message says to use the full data repo checkout); a drifted
table with rows but no `snapshot_date date` column also fails. (2) `parse_json` catches
`RecursionError`; NUL and lone-surrogate check (iterative, keys and values) in validation; `main`
has a final `except Exception` (type only + "load rolled back"), also around validation and
connect. (3) zero-byte `.jsonl.gz` rejected as invalid gzip. (4) tests: 30-digit exact decimal vs
`payload->>'price'`, COPY-escaping round-trip (tab, newline, CR, backslash, literal `\N`, quotes,
non-ASCII, raw tab whitespace, surrogate pair) in a typed column and the payload, both review
reproductions + dropped table, orphans from file tables only, real 22P02 through `main` with no row
content in `caplog`, unexpected exceptions in both phases. 13 of the new tests fail on the round-0
code. (5) §1b payload wording and the orphans-plus-drift rule in §1b and ADR-015 (edited in place).
PLAN 1.9 added (reviewer side note: lone surrogates in the snapshot writer). `ruff check .` clean;
full `pytest` 271 passed, 2 skipped (Olist real data), PostgreSQL 16.

### 2026-11-04 — orchestrator — Phase 1 review, Phase 2 plan
Done: owner approved Phase 1 (1.3 stays open until 3 consecutive scheduled successes; 1.6/1.8
alongside Phase 2). Owner chose Neon (managed Postgres) for the warehouse and dbt for transformations:
ADR-014. PLAN gains 2.0 (dbt skeleton + CI), 2.5 (ML snapshot raw loader) and 2.6 (Neon load workflow).
Review round 1 (PR #10): owner chose an Environment `warehouse` (main only) for the Neon secret,
reuse of `DATA_REPO_PAT` to read the data repo (extends ADR-010), and the Olist zip in the data repo.
ADR-014 now pins schema names, secret handling (mask every URL part), storage/CU estimates, API
exposure (marts only, read-only role) and the data repo as system of record.
Not done / open: Neon project, Environment `warehouse` + `WAREHOUSE_DATABASE_URL`, `archive.zip` in the data repo (owner). The 2026-11-04 05:23 UTC
scheduled snapshot did not fire; owner chose to wait for the next one.
Gotchas for the next agent: never run dbt or loaders against Neon from the workspace (it has no
secret and no route); develop against a throwaway Postgres. dbt reads raw schemas as sources only.
Files touched: docs/DECISIONS.md, docs/PLAN.md, docs/PROGRESS.md

### 2026-11-04 — orchestrator — 1.5 closed
Done: PR #8 merged (squash 541596b) after review round 3 (APPROVE) and green CI. First live
*Enrichment load* run 41806204617 succeeded: `raw_enrichment.fx_rates` 5208 rows (BRL, USD,
requested range 2016-09-01..2026-11-04; the last loaded date is probably 2026-11-03, as the ECB
publishes after 16:00 CET); `raw_enrichment.public_holidays` 74 rows (BR 2016–2018: 14 each; AR
2026–2027: 16 each). Frankfurter v1 and Nager.Date v3 payloads matched the loaders' strict
validation. CLAUDE.md commands rows added for both loaders and the workflow. PLAN: 1.1, 1.2, 1.4,
1.5 done; 1.8 added for the round-3 low findings. Acceptance wording of 1.4/1.5 aligned with the
contract (data lands in `raw_*`; `staging` is a later layer).
Not done / open: Olist Kaggle API download mode still never run live (only `--from-zip`). 1.3 (the 05:23 UTC schedule did not fire on its first day; owner decides on a manual run).
Gotchas for the next agent: the enrichment workflow loads into a throwaway Postgres; there is no
persistent warehouse yet, so Phase 2 must decide where the warehouse lives before loading for real.
Files touched: CLAUDE.md, docs/PLAN.md, docs/PROGRESS.md

### 2026-11-03 — data-engineer — 1.5 (PR #8, branch `ingestion/enrichment`, after review round 1)
Done: `python -m ingestion.enrichment.fx_rates` (ECB rates EUR→BRL,USD from Frankfurter **v1**,
2016-09-01 → today, one request per calendar year, payload + density validation, `Decimal` rates)
and `python -m ingestion.enrichment.holidays` (Nager.Date **v3**; BR 2016–2018, AR 2026 → next year;
`--only CC:YYYY[-YYYY]`). Both load `raw_enrichment` in one transaction (delete requested range/pairs,
`COPY` the fetched rows, audit row in `raw_enrichment._load_audit`); `--csv-out` fetches without a DB.
Shared `ingestion/enrichment/http.py`: sequential, 0.5 s delay, request cap, backoff on 429/5xx/network
(numeric `Retry-After`). Manual workflow `.github/workflows/enrichment-load.yml` (workflow_dispatch
only, no secrets): runs both against the live APIs into a Postgres service, writes row counts to
the job summary, uploads the CSVs as an artifact. ADR-012 (FX), ADR-013 (Nager v3); DATA_CONTRACT §2b.
Review round 1: Frankfurter snaps a non-publication start day back to the previous one (2024-01-01 →
2023-12-29), which made every chunk after the first fail; leading dates are now dropped (debug log)
and the fake API models the snap. Holidays moved from v4 (unverified) to v3 with strict validation
(a v4-shaped answer fails). `InvalidOperation` → `FetchError`. `raw_enrichment` tables are re-created
on column drift (names/order/types). PLAN 1.6 (Olist download hardening) and 1.7 (Nager v4) added.
Rebased onto `main` after PR #7 merged. `ruff check` clean; `pytest`: 202 passed, 2 skipped
(Olist real-data tests), with a local PostgreSQL 16.
Review round 2: `store.ensure_schema(cur, target)` re-creates only the loader's own table on drift;
other missing tables are created, other drifted data tables are left alone (warning), and a drifted
`_load_audit` raises `SchemaDriftError` (exit 1, rollback, history kept; never dropped). FX density is
checked per `quote_currency`; a date more than 7 days before a chunk start is a `FetchError`
(DATA_CONTRACT §2b updated). `ruff check` clean; `pytest`: 207 passed, 2 skipped (PostgreSQL 16).
Not done / open: **never run live** (APIs blocked here): after merge run *Enrichment load (manual)*
once (`gh workflow run enrichment-load.yml --ref main`) and check: BRL/USD ≈ 2,600+ days each from
2016-09-01; roughly 10–20 BR/AR entries per year. ARS deliberately not loaded (not an ECB currency).
No persistent warehouse exists yet, so nothing keeps these rows between runs (the artifact does).
Gotchas for the next agent: the task text named `api.frankfurter.app`; v1 lives at
`api.frankfurter.dev/v1` (ADR-012). Holidays: no PK, all types kept: filter
`'Public' = ANY(holiday_types)` and use `is_global` / `counties` (null = nationwide) in staging;
v3 is EOL 2027-01-31 (PLAN 1.7). FX has no rows on weekends/TARGET holidays.
`db_conn` test fixture is autocommit (loader transactions are real, not savepoints).
CLAUDE.md Commands rows for 1.5 handed to the orchestrator.
Files touched: ingestion/enrichment/**, tests/enrichment/**, tests/conftest.py,
.github/workflows/enrichment-load.yml, README.md, docs/DATA_CONTRACT.md, docs/DECISIONS.md,
docs/PLAN.md, docs/PROGRESS.md

### 2026-11-03 — data-engineer — 1.4 (branch `ingestion/olist-load`)
Done: `python -m ingestion.olist.download` (Kaggle REST endpoint with `KAGGLE_USERNAME`/`KAGGLE_KEY`
as Basic auth, 429/5xx/network retried with backoff, or `--from-zip`). Accepts only an archive
holding exactly the nine CSVs at its root; extracts to a temp folder, verifies CRC, writes
`manifest.json` (SHA-256 + bytes per file) last. `python -m ingestion.olist.load` validates every
file first (header = contract, record arity, SHA-256 vs manifest, records counted with `csv`), then
loads all nine `raw_olist` tables (all `text`) in **one transaction**: truncate + `COPY` + count check +
`raw_olist._load_audit` row per table. `ingestion/db.py` reads `DATABASE_URL` (never logged).
`tests/conftest.py`: throwaway DB per session (on `DATABASE_URL`, else an auto-started `initdb`
cluster, else skip). CI `python` job gets a `postgres:16` service + `DATABASE_URL`. psycopg 3: ADR-011.
Verified on the real Kaggle archive (44,717,580 bytes): download `--from-zip` OK, full load ~3 s,
DB counts = CSV records for all nine tables (contract §2), rerun idempotent, BOM stripped.
Not done / open: Kaggle API mode never run live (kaggle.com blocked here; mocked only).
CLAUDE.md Commands rows for 1.4 handed to the orchestrator (agents don't edit CLAUDE.md).
Gotchas for the next agent: the Olist CSVs are in `data/olist` (git-ignored) or wherever `--dest`
pointed. Staging must rename `*_lenght` → `*_length`; empty unquoted fields are NULL in raw.
The loader re-creates a raw table without `CASCADE` when its columns drift, so staging views on raw
tables block such a change (on purpose). Tests: no `__init__.py` in test dirs, so test file names
must be unique repo-wide. `initdb` refuses root: as root, set `DATABASE_URL`.
Files touched: ingestion/db.py, ingestion/olist/**, tests/conftest.py, tests/olist/**,
.github/workflows/ci.yml, requirements.txt, pyproject.toml, README.md, docs/DATA_CONTRACT.md,
docs/DECISIONS.md, docs/PLAN.md, docs/PROGRESS.md

### 2026-11-03 — orchestrator — 1.3 first live run
Done: first real snapshot (run 41729877814): `success`, 195 requests, 100 highlights, 87 products,
2,313 offers, 50 trends, 0 errors; skipped highlights ITEM 3 / USER_PRODUCT 10. Rotated refresh token
saved to `ML_REFRESH_TOKEN`; day folder pushed to the private data repo.
Not done / open: 2 more consecutive daily successes before 1.3 can be marked done.
Gotchas: the first two runs failed with exit 2 (`invalid_grant`), most likely because the stored
refresh token had been issued while authorizing with the pre-renewal client secret. Re-authorizing
with the new secret fixed it (see docs/API_PROBE.md "Token behaviour"). Run `Check secrets` before
spending a refresh token (it validates the client credentials, not the stored refresh token; after a
client secret renewal, re-authorize regardless).
Files touched: docs/PROGRESS.md, docs/API_PROBE.md

### 2026-11-03 — orchestrator — pre-flight for 1.3 (PR #4)
Done: manual `Check secrets` workflow. Proves, without printing values or spending the refresh token:
secrets present/well-formed; ML client id/secret accepted (fake refresh token → `invalid_grant` = valid
client, `invalid_client` = wrong secret; verified live); data repo is private; PAT can write Actions
secrets (writes and deletes a throwaway `CHECK_SECRETS_PROBE`); PAT can push to the data repo
(`git push --dry-run`, auth via env-provided header, never in URL/argv).
Gotchas: checkout must use `persist-credentials: false`, otherwise GITHUB_TOKEN shadows the PAT.
ADR-010 records the owner's accepted risks (single PAT, no Environment).
Files touched: .github/workflows/check-secrets.yml, scripts/check_secrets.py, tests/test_check_secrets.py, docs/DECISIONS.md, CLAUDE.md

### 2026-11-03 — devops — 1.3 (branch `devops/ml-daily-snapshot`)
Done: `.github/workflows/ml-daily-snapshot.yml`: daily at 05:23 UTC + `workflow_dispatch`, main only,
`permissions: contents: read`, job-level `concurrency` group keyed by ref, no cancel-in-progress, job
timeout 50 min (snapshot step 40 min). Checks out both repos with `persist-credentials: false` (data
repo into `data/` via `DATA_REPO_PAT`; push auth only in the push step, via env-only git config), runs the
snapshot with `--out data --date <UTC today> --new-refresh-token-file $RUNNER_TEMP/ml_refresh_token`,
captures the exit code as a step output. Token step (`always()`, the only one): saves the file to
`ML_REFRESH_TOKEN` via `gh secret set` (3 attempts, value masked, file shredded) only if it exists.
Commit step: only on exit 0/1 reported by the step, only if the day's `run.json` exists, refuses
commits that delete tracked files (`--no-renames`, so a deleted day can't pass as a rename), skips when
nothing changed; bot identity exported for the whole step (commit and rebase-retry); message `snapshot YYYY-MM-DD (status)`.
Last step fails the job with a human message per exit code. `scripts/summarize_run.py` (stdlib only)
writes the `$GITHUB_STEP_SUMMARY` table (counts only) and `::warning::` annotations (no offers for any
followed product / offers 0 with products > 0, partial, request cap reached); 19 new tests (113 total).
ADR-009 records the step conditions and the commit guard. actionlint 1.7.7 + shellcheck clean;
token and commit steps dry-run locally against a stub `gh` and a scratch git remote.
Review round 1 (PR #5): deletion guard made rename-proof, identity for the rebase retry, concurrency
moved to the job and keyed by ref, data checkout no longer persists the PAT.
Not done / open: never run on GitHub (needs the secrets). PLAN 1.3 stays `in review` at merge; its
acceptance (3 consecutive successful daily runs) is checked after merge. The single broad PAT and the
lack of a GitHub Environment are owner-accepted risks (ADR-010, separate PR).
Gotchas for the next agent: the workflow only becomes dispatchable once it is on `main`. If the data
contract adds/renames/drops a snapshot file, update the deletion guard in the commit step (ADR-009).
`scripts/` is imported in tests as `scripts.summarize_run` (namespace package via `pythonpath`);
don't add a `tests/scripts/` package (name clash).
Files touched: .github/workflows/ml-daily-snapshot.yml, scripts/summarize_run.py,
tests/test_summarize_run.py, CLAUDE.md (Commands rows, applied by the orchestrator), docs/DECISIONS.md,
docs/PLAN.md, docs/PROGRESS.md

### 2026-11-03 — data-engineer — 1.2 (PR #3, after review round 2)
Done: `python -m ingestion.mercadolibre.snapshot`. Refreshes the token (transient 429/5xx/network
failures retried with backoff), writes the rotated refresh token to `--new-refresh-token-file` before
any data call, then trends → highlights per category (all types written, only PRODUCT followed) →
product + paginated offers (page 50, cap 100/product, deduped by item_id, never offset >= total) and
writes `mercadolibre/snapshot_date=.../` per DATA_CONTRACT §1 via temp folder + rename.
Review fixes: malformed payloads and per-unit exceptions are contained and recorded as `{path, status:
null}`; stale `.tmp-*`/`.old-*` cleaned (or restored) at start and git-ignored at the storage root;
old folder deleted only after the swap succeeds; replacement rule success > partial > failed;
`categories` = captured, new `categories_requested` and `skipped_highlights`; highlights gain `type`;
offers fetched before hitting the cap are kept; client catches any `httpx.HTTPError` and records a
200-with-null body; distinct exit codes (2 re-auth, 4 transient token failure, 5 config).
Round 2: empty highlights are not a captured category (error entry; all empty → `failed`, never
replaces a good day); first-page 404 on offers = no offers, counted in `products_without_offers`;
pagination stops when a page adds no new item_id; abort log names the failing step; ADR-007
(replacement rule) and ADR-008 (exit codes) added; stale category table removed from the contract.
All five default categories verified live by the orchestrator (docs/API_PROBE.md follow-up section).
Not done / open: never run live from here; the first scheduled run is the real end-to-end test.
Gotchas for the next agent (task 1.3): exit codes are in ADR-008 / CLAUDE.md.
(a) Only the token-save step may run with `if: always()`. The data commit must run only on exit 0 or 1;
otherwise a run killed mid-swap could commit a deleted day folder.
(b) The workflow needs a `concurrency:` group (no overlapping runs): `cleanup_stale` of one run can
break another run's swap.
(c) Save the token file only when it exists (exit 0/1). On exit 4, retry later (old token probably
still valid; if the retry gets exit 2, the owner re-authorizes). Exit 2/3/5 = alert the owner.
Also: keep the token file outside the data repo checkout, and keep the `.gitignore` the job writes
at `--out` committed in the data repo.
Files touched: ingestion/mercadolibre/{snapshot,auth,client,authorize}.py, tests/mercadolibre/**,
docs/DATA_CONTRACT.md, docs/API_PROBE.md, docs/DECISIONS.md, CLAUDE.md, docs/PLAN.md, docs/PROGRESS.md

### 2026-11-03 — data-engineer — 1.1 (branch `ingestion/ml-oauth`)
Done: `ingestion/mercadolibre/` package. `auth.py` refreshes with `grant_type=refresh_token` and returns
the rotated refresh token (`TokenPair`); `invalid_grant` raises `ReauthorizationRequired` with re-auth
instructions. `authorize.py` is the one-time PKCE (S256) bootstrap for the owner. `client.py` is a
sequential GET client: Bearer header, 0.5 s delay, backoff on 429/5xx/transport errors (max 3 retries,
honours numeric `Retry-After`), hard request cap (retries count), errors recorded as `{path, status}`.
Tests use `httpx.MockTransport` only (no extra deps). CI now installs `requirements.txt`.
Not done / open: never run against the live API (unreachable from the workspace). The owner should run
`python -m ingestion.mercadolibre.authorize` once to validate the PKCE flow end to end.
Gotchas for the next agent: transport errors are recorded with `status: null`. `RequestCapReached` is an
exception, not a recorded error: callers decide how to finish the run. `pytest` imports `ingestion`
via `pythonpath = ["."]` in pyproject (no package install needed).
Files touched: ingestion/**, tests/mercadolibre/**, pyproject.toml, requirements.txt,
.github/workflows/ci.yml (install step only), CLAUDE.md, docs/PLAN.md, docs/PROGRESS.md

### 2026-11-03 — devops — 0.3 (PR #1)
Done: secret scan now runs the gitleaks CLI (checksum-verified) over the full history; actions moved to Node 24 versions.
Gotchas: `gitleaks/gitleaks-action@v2` fails on the first push of a new repo (it diffs `<root>^..<head>`; the root has no parent). Don't switch back to it.
Files touched: .github/workflows/ci.yml

### 2026-11-03 — orchestrator — 0.1, 0.2, 0.3
Done: repo scaffold, project memory docs, agent role definitions, CI workflow.
Verified the Mercado Libre API by hand (see docs/API_PROBE.md): auth works with PKCE; catalog,
best sellers, offers per product and trends work; general search and item detail are blocked.
ADR-003 decided: raw snapshots go to the private repo `ecommerce-analytics-data`.
Gotchas: the ML app enforces PKCE; refresh tokens rotate. Nothing that calls the ML API can be run
from the agents' cloud workspace — use mocks/fixtures.
