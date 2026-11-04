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
| 1.1 | ML OAuth client with PKCE + refresh rotation | data-engineer | in review | Unit tests with mocked HTTP; never logs tokens |
| 1.2 | ML daily snapshot job (best sellers → products → offers, 5 categories) | data-engineer | in review | Writes files matching DATA_CONTRACT; idempotent per day; request cap |
| 1.3 | Scheduled workflow running 1.2 daily + persisting rotated refresh token | devops | in review | 3 consecutive successful daily runs |
| 1.4 | Olist download + load into PostgreSQL staging | data-engineer | in review | Row counts match source; reproducible from one command |
| 1.5 | Enrichment: FX rates (ECB) and holidays (Nager.Date) | data-engineer | in review | Loaded to staging with tests |
| 1.6 | Olist download hardening (PR #7 low findings) | data-engineer | todo | Hash bytes while streaming the `COPY` instead of a second read (load.py:106,166); byte cap on the HTTP download (download.py:93-95); map `RuntimeError`/`NotImplementedError` from `zipfile` (encrypted/unsupported members) to `DownloadError` (download.py:151); `cleanup_stale` deletes only files the tool created (download.py:175-182); note/resolve config exit code 2 (Olist, enrichment) vs 5 (ML snapshot) |
| 1.7 | Migrate holidays to Nager.Date v4 (before v3 EOL 2027-01-31) | data-engineer | todo | v4 endpoint and fields verified by a live run; loader, DATA_CONTRACT §2b and ADR updated in one PR |

## Phase 2 — Model and metrics

| # | Task | Owner | Status | Acceptance criteria |
|---|------|-------|--------|---------------------|
| 2.1 | Star schema for Olist (facts: order_items, deliveries; dims: date, customer, seller, product, geo) | analytics-engineer | todo | Documented in DATA_CONTRACT; data tests pass |
| 2.2 | ML price-history model (fact_offer_snapshot, dim_product, dim_seller, dim_category) | analytics-engineer | todo | One row per offer per day; tests pass |
| 2.3 | Business metric marts (revenue, delivery delay, review impact, cohorts, price dynamics) | analytics-engineer | todo | Each metric has a definition in DATA_CONTRACT |
| 2.4 | API serving the marts | backend | todo | Typed endpoints, tests, OpenAPI docs |

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
