# Plan

Each phase ends with a review by the project owner (Nadir). Each task is one PR.
Status: `todo` · `in progress` · `in review` · `done`.

## Phase 0 — Foundations

| # | Task | Owner | Status | Acceptance criteria |
|---|------|-------|--------|---------------------|
| 0.1 | Repo memory: CLAUDE.md, PLAN, DATA_CONTRACT, DECISIONS, PROGRESS | orchestrator | done | Files exist and are consistent |
| 0.2 | Agent role definitions in `.claude/agents/` | orchestrator | done | One file per role, each with scope and boundaries |
| 0.3 | CI: lint + tests on every PR | devops | done | Workflow runs green on `main` |
| 0.4 | Decide where raw ML snapshots are stored | owner | done | ADR-003 has a chosen option |

## Phase 1 — Data in (priority: start the Mercado Libre history as early as possible)

| # | Task | Owner | Status | Acceptance criteria |
|---|------|-------|--------|---------------------|
| 1.1 | ML OAuth client with PKCE + refresh rotation | data-engineer | done | Unit tests with mocked HTTP; never logs tokens |
| 1.2 | ML daily snapshot job (best sellers → products → offers, 5 categories) | data-engineer | done | Writes files matching DATA_CONTRACT; idempotent per day; request cap |
| 1.3 | Scheduled workflow running 1.2 daily + persisting rotated refresh token | devops | in review | 3 consecutive successful daily runs |
| 1.4 | Olist download + load into PostgreSQL `raw_olist` | data-engineer | done | Row counts match source; reproducible from one command (`--from-zip` path verified on the real archive; Kaggle API mode unverified live) |
| 1.5 | Enrichment: FX rates (ECB) and holidays (Nager.Date) | data-engineer | done | Loaded to `raw_enrichment` with tests; live run 41806204617 |
| 1.6 | Olist download hardening (PR #7 low findings) | data-engineer | todo | Hash bytes while streaming the `COPY` instead of a second read (load.py:106,166); byte cap on the HTTP download (download.py:93-95); map `RuntimeError`/`NotImplementedError` from `zipfile` (encrypted/unsupported members) to `DownloadError` (download.py:151); `cleanup_stale` deletes only files the tool created (download.py:175-182); note/resolve config exit code 2 (Olist, enrichment) vs 5 (ML snapshot) |
| 1.7 | Migrate holidays to Nager.Date v4 (before v3 EOL 2027-01-31) | data-engineer | todo | v4 endpoint and fields verified by a live run; loader, DATA_CONTRACT §2b and ADR updated in one PR |
| 1.8 | Enrichment hardening (PR #8 round-3 low findings) | data-engineer | todo | `store.ensure_schema` checks `_load_audit` before touching the target table (store.py:107-130); drift check also covers constraints (PK, NOT NULL, CHECK) or §2b states the gap; test that 8 days before a chunk start is rejected (`test_date_far_before_the_chunk_start_is_an_error`, test_fx_rates.py:170-177); the load log reports the actual min/max dates loaded, not the requested range (fx_rates.py:298); audit-drift holiday test asserts the earlier rows survive (test_holidays.py:256-276) |

## Phase 2 — Model and metrics

Order: 2.0 and 2.5 in parallel → 2.6 (needs the owner's Neon setup) → 2.1 → 2.2 → 2.3 → 2.4. Tasks 1.6 and 1.8 run alongside.

| # | Task | Owner | Status | Acceptance criteria |
|---|------|-------|--------|---------------------|
| 2.0 | dbt project skeleton in `warehouse/` + CI `dbt build` on a throwaway Postgres (ADR-014) | analytics-engineer | todo | `dbt build` green in CI on fixtures with no secrets; sources declared for `raw_olist` and `raw_enrichment`; profile reads env only; schemas are exactly `staging`/`core`/`marts` (`generate_schema_name` overridden); dbt pinned in `requirements-dbt.txt` |
| 2.5 | Load ML snapshots (data repo) into `raw_mercadolibre` | data-engineer | todo | New DATA_CONTRACT §1b `raw_mercadolibre` in the same PR (golden rule); idempotent per `snapshot_date`; loads every day folder found; tests on fixture snapshots |
| 2.6 | Warehouse on Neon: load workflow (raw loads + `dbt build`) using `WAREHOUSE_DATABASE_URL` | devops | todo | Owner prerequisites: Neon project, Environment `warehouse` (main only) with `WAREHOUSE_DATABASE_URL`, `archive.zip` at `olist/archive.zip` in the data repo (path recorded in DATA_CONTRACT in this PR). Job runs only on `main` in that Environment, with a job-level `concurrency` group (`cancel-in-progress: false`, as ADR-009); URL split in Python with `::add-mask::` on every part; reads data repo with `DATA_REPO_PAT`; daily runs load only `raw_mercadolibre` then `dbt build`; `raw_olist` (`--from-zip`, records the archive sha256) and `raw_enrichment` load on manual dispatch or when their input changes; first full run builds green on the real data; summary shows row counts and `pg_database_size`; scheduled after the daily ML snapshot (or chained) so the warehouse stays current |
| 2.1 | Star schema for Olist (facts: order_items, deliveries; dims: date, customer, seller, product, geo) | analytics-engineer | todo | Documented in DATA_CONTRACT; dbt tests pass on fixtures in CI **and** on the full real load (2.6 run) |
| 2.2 | ML price-history model (fact_offer_snapshot, dim_product, dim_seller, dim_category) | analytics-engineer | todo | Declares the `raw_mercadolibre` source; one row per offer per day; tests pass; not exposed by the API (ADR-014) |
| 2.3 | Business metric marts (revenue, delivery delay, review impact, cohorts, price dynamics) | analytics-engineer | todo | Each metric has a definition in DATA_CONTRACT |
| 2.4 | API serving the marts | backend | todo | Typed endpoints, tests, OpenAPI docs; connects with a read-only role limited to `marts`, never the loader credential (ADR-014) |

## Phase 3 — Dashboard

| # | Task | Owner | Status | Acceptance criteria |
|---|------|-------|--------|---------------------|
| 3.1 | React + TS app shell, routing, design tokens | frontend | todo | Builds in CI |
| 3.2 | Business performance views (Olist) | frontend | todo | Uses API only |
| 3.3 | Price intelligence views (Mercado Libre) | frontend | todo | Uses API only |

## Phase 4 — Ship it

| # | Task | Owner | Status | Acceptance criteria |
|---|------|-------|--------|---------------------|
| 4.1 | Deploy (AWS free tier) | devops | todo | Public URL works |
| 4.2 | README with business questions, findings, architecture diagram | orchestrator | todo | A reviewer understands the project in 2 minutes |

## Then: Project 2

Backend service (orders/inventory API) that emits events into this platform. Planned after Phase 4.
