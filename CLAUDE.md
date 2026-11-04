# CLAUDE.md — read this first

This file is the entry point for every agent (and human) working on this repo.
Agents start with no memory: **everything you need to know lives in this file and in `docs/`.**

## What this project is

**ecommerce-analytics** is a portfolio project that combines two data sources into one
marketplace-intelligence platform:

1. **Business performance (historical)** — the public Olist Brazilian e-commerce dataset
   (~100k orders, 2016–2018): orders, items, payments, deliveries, reviews, sellers.
2. **Competitive pricing (live)** — daily snapshots from the official Mercado Libre API
   (Argentina, site `MLA`): best sellers per category, catalog products and **every offer**
   on each product (price, original price, seller, shipping, logistics, official store).

Story: *"How does a marketplace perform, and how do sellers compete on price and delivery?"*

Target audience: reviewers for Amazon internships (BI Engineer, SDE, TPM, Business Analyst).
That means: rigorous SQL, clean pipelines, tests, CI, and analyses that answer **business questions**,
not analyses for their own sake.

## Session start checklist (every agent, every time)

1. Read `docs/PROGRESS.md` (current state + latest handoff).
2. Read `docs/PLAN.md` and find the task you were assigned.
3. Read `docs/DATA_CONTRACT.md` before touching any schema, table, file layout or API payload.
4. Skim `docs/DECISIONS.md` so you don't re-open settled questions.

## Session end checklist

1. Append a handoff entry to `docs/PROGRESS.md` (template inside that file).
2. If you made a non-trivial technical choice, add an entry to `docs/DECISIONS.md`.
3. If you changed a schema or payload, update `docs/DATA_CONTRACT.md` **in the same PR**.

## Golden rules (non-negotiable)

- **Secrets never enter the repo.** No client secret, access token, refresh token or PAT in code,
  config, tests, fixtures, logs, commit messages or PR text. Read them from environment variables /
  GitHub Actions secrets. `.env` and `*.tokens.json` are git-ignored.
- **Tokens go in the `Authorization: Bearer` header, never in a URL query string.**
- **Never log token values.** Log only status codes, endpoint paths and counts.
- **Raw Mercado Libre data is not committed to this public repo** (see DECISIONS ADR-003).
  Aggregated, analysis-level outputs are fine.
- **The data contract is the source of truth.** Don't invent column names; if you need a new one,
  change the contract first.
- **Small PRs, one task each.** Branch name: `<area>/<short-task>` (e.g. `ingestion/ml-snapshot`).
- **Every PR passes CI and is reviewed by the `reviewer` agent before merge.** Agents never review their own work.
- Write code, comments, docs and commit messages in **English**.

## Mercado Libre API — facts verified on 2026-11-03 (see docs/API_PROBE.md)

- OAuth 2.0 **authorization code + PKCE (S256)** + refresh token. Access token lives 6 h.
  **The refresh token rotates on every use: always persist the new one.**
- Working: `/sites/MLA/categories`, `/categories/{id}`, `/highlights/MLA/category/{cat}`,
  `/products/search`, `/products/{id}`, `/products/{id}/items`, `/trends/MLA`.
- Blocked (403, do not build on them): `/sites/MLA/search`, `/items/{id}`, `/items/{id}/prices`.
- `/products/{id}/items` pages with `offset`/`limit`; an offset at or beyond `paging.total` returns 404.
- Highlights `content[].type` is `PRODUCT`, `USER_PRODUCT` or `ITEM`; only `PRODUCT` can be followed.
- There is **no sales/units data**. Ranking position in best sellers is our demand proxy.
- Be polite: sequential requests, small delay, retry with backoff on 429/5xx, hard cap per run.

## Repo map

```
ingestion/mercadolibre/   Python: OAuth, daily snapshot job
ingestion/olist/          Python: download + load Olist CSVs
warehouse/                SQL: staging → star schema → marts (PostgreSQL)
api/                      Backend serving metrics to the dashboard
dashboard/                React + TypeScript dashboard
docs/                     Project memory (plan, contract, decisions, progress)
.claude/agents/           Agent role definitions for this repo
.github/workflows/        CI and scheduled jobs
```

## Commands

Filled in as each area is built. Keep this section accurate.

| Task | Command |
|------|---------|
| Lint Python | `ruff check .` |
| Test Python | `pytest` |
| Install Python deps | `pip install -r requirements.txt` |
| ML one-time authorization (owner, interactive; needs env `ML_CLIENT_ID`, `ML_CLIENT_SECRET`, `ML_REDIRECT_URI`) | `python -m ingestion.mercadolibre.authorize` |
| ML daily snapshot (env `ML_CLIENT_ID`, `ML_CLIENT_SECRET`, `ML_REFRESH_TOKEN`) | `python -m ingestion.mercadolibre.snapshot --out <storage-root> --new-refresh-token-file <path> [--date YYYY-MM-DD] [--categories ...]` |
| Trigger the scheduled ML snapshot manually (runs daily at 05:23 UTC; main only) | `gh workflow run ml-daily-snapshot.yml --ref main` (watch: `gh run watch`) |
| Render a run's job summary / exit-code message locally | `python scripts/summarize_run.py summary --run-json <path>/run.json --exit-code <code>` · `python scripts/summarize_run.py check --exit-code <code>` |
| Check the Actions secrets (manual, never prints values, never spends the refresh token) | Actions tab → *Check secrets* → Run workflow, or `gh workflow run check-secrets.yml --ref main` |
| Download Olist CSVs (Kaggle REST, env `KAGGLE_USERNAME`, `KAGGLE_KEY`; or a local zip) | `python -m ingestion.olist.download --dest data/olist [--from-zip <path>]` |
| Load Olist CSVs into `raw_olist` (env `DATABASE_URL`) | `python -m ingestion.olist.load --source data/olist` |
| Load ECB FX rates (EUR→BRL,USD, Frankfurter v1) into `raw_enrichment.fx_rates` (env `DATABASE_URL`, not needed with `--csv-out` only) | `python -m ingestion.enrichment.fx_rates [--start 2016-09-01] [--end YYYY-MM-DD] [--csv-out <file>]` |
| Load public holidays (Nager.Date v3; BR 2016–2018, AR 2026→next year) into `raw_enrichment.public_holidays` (env `DATABASE_URL`, not needed with `--csv-out` only) | `python -m ingestion.enrichment.holidays [--only CC:YYYY[-YYYY]] [--csv-out <file>]` |
| Run both enrichment loads live into a throwaway Postgres (manual, no secrets) | `gh workflow run enrichment-load.yml --ref main` (watch: `gh run watch`) |
| Load ML snapshots from the data repo into `raw_mercadolibre` (env `DATABASE_URL`; skips unchanged days, ADR-015) | `python -m ingestion.mercadolibre.load --source <data-repo-root> [--full-reload]` |
| Install dbt (warehouse) | `pip install -r requirements-dbt.txt` |
| Load warehouse CI fixtures into a throwaway DB (env `DATABASE_URL`; opt-in `FIXTURE_DB_ALLOW=1`; refuses a DB with more rows than the fixtures; never the real warehouse) | `FIXTURE_DB_ALLOW=1 python -m warehouse.ci.fixture_db load --inventory <file>` |
| dbt build (env `DBT_PG_HOST`, `DBT_PG_PORT`, `DBT_PG_USER`, `DBT_ENV_SECRET_PG_PASSWORD`, `DBT_PG_DBNAME`, `DBT_PG_SSLMODE`; all optional, local defaults) | `dbt build --project-dir warehouse --profiles-dir warehouse` |
| Check dbt created only `staging`/`core`/`marts` and left `raw_*` and `public` unchanged (fixture DB only) | `python -m warehouse.ci.fixture_db check --inventory <file>` |
| Test the dbt schema guards (needs dbt) | `pytest tests/warehouse` |

Olist, enrichment and ML raw loaders exit 0 ok, 1 fetch/validation/load failed (nothing changed), 2 configuration error.

Snapshot exit codes (ADR-008; the scheduled workflow branches on these):

| Code | Meaning | Rotated refresh token file written? | Action |
|------|---------|-------------------------------------|--------|
| 0 | `success` or `partial` (data written; see `run.json`) | yes | save token |
| 1 | `failed` (no category captured, or aborted after the token was saved) | yes | save token; investigate |
| 2 | re-authorization required (`invalid_grant`, or token endpoint rejected the credentials) | no | owner runs `python -m ingestion.mercadolibre.authorize` |
| 3 | new refresh token could not be persisted | no | owner must re-authorize (old token already consumed) |
| 4 | token endpoint unavailable after retries (429/5xx/network) | no | retry later: the old token is *probably* still valid; if the next run gets `invalid_grant` (exit 2), re-authorize |
| 5 | configuration error (missing env vars) | no | fix the secrets/env |
