# Progress

Current state at the top; handoff log below (newest first).

## Current state

- **Phase:** 1 — Data in
- **Next up:** watch the daily runs — 1.3 needs 3 consecutive daily successes (1 of 3 so far: run 41729877814; the first scheduled run, 2026-11-04 05:23 UTC, did not fire). Then Phase 2 (2.1 star schema), plus small tasks 1.6, 1.8 and 1.7 (before 2027-01-31).
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
