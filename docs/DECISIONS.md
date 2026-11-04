# Decisions

Lightweight architecture decision records. Newest at the bottom.
Format: context → decision → consequences. Don't edit past entries; supersede them with a new one.

---

## ADR-001 — Hybrid data sources (2026-11-03)

**Context.** The Mercado Libre API gives real, live catalog and offer data but no sales,
and its history starts the day we start capturing. Classic BI metrics (revenue, cohorts,
delivery performance) need order data.

**Decision.** Use Olist (historical orders) for business-performance analytics and the
Mercado Libre API (live daily snapshots) for competitive-pricing analytics.

**Consequences.** Two pipelines and two sub-models. The dashboard has depth from day one
(Olist) while the ML history accumulates. Both are Latin American marketplaces, so the story is coherent.

## ADR-002 — English for everything in the repo (2026-11-03)

**Decision.** Code, comments, docs and commits are in English. Reviewers are Amazon recruiters/engineers.

## ADR-003 — Raw Mercado Libre data stays out of the public repo (2026-11-03)

**Context.** The repo is public from day one. Raw snapshots are third-party data obtained
through the official API under its terms; republishing them wholesale is not our purpose.

**Decision.** Raw snapshots are never committed here. Only code, schemas, and aggregated outputs are.

**Where raw snapshots live — decided: (a) private companion repo `nneme-dotcom/ecommerce-analytics-data`.** Options considered:
- (a) A private companion repo written by the scheduled workflow (free, simple, needs a fine-grained PAT).
- (b) AWS S3 (free tier, production-like, needs an AWS account now instead of in Phase 4).
- (c) A managed Postgres free tier (e.g. Neon/Supabase), loading snapshots directly.

Why (a): free, zero new accounts, versioned history of every snapshot. The scheduled workflow
pushes with a fine-grained PAT scoped to that repo only. Can migrate to S3 in Phase 4 without changing the layout.

## ADR-004 — OAuth with PKCE and rotating refresh tokens (2026-11-03)

**Context.** The Mercado Libre app has PKCE enforced (`code_verifier` is required).
Refresh tokens rotate on every use.

**Decision.** One-time interactive authorization by the account owner; afterwards the job
only uses `grant_type=refresh_token` and must persist the new refresh token after every run.

**Consequences.** If persisting the new token fails, the next run fails and the owner must re-authorize.
The job must treat "token persisted" as part of success.

## ADR-005 — Ingestion runs in GitHub Actions (2026-11-03)

**Context.** The agents' cloud workspace cannot reach `api.mercadolibre.com`.

**Decision.** Scheduled ingestion runs in GitHub Actions. Agents develop against mocked HTTP and recorded fixtures.

## ADR-006 — Dashboard in React + TypeScript (2026-11-03)

**Decision.** Custom dashboard instead of Metabase/Superset, to also demonstrate frontend engineering.

## ADR-007 — Snapshot replacement rule (2026-11-03)

**Context.** The daily snapshot job may be re-run for the same `snapshot_date` (manual re-run,
retry after an incident). A worse re-run must not destroy a better snapshot that already exists.

**Decision.** Rank run statuses `success` > `partial` > `failed`. A run replaces the existing day
folder only if its status ranks at least as high (so `partial` never replaces `success`; `partial`
may replace `partial` or `failed`; `failed` only replaces `failed`). Empty data never counts as
success: a category is captured only if its highlights return at least one entry, and a run with no
captured category is `failed`. Products without offers (first-page 404 or `total: 0`) are not errors.

**Consequences.** Re-runs are safe. A day can only get better. Details in DATA_CONTRACT §1.

## ADR-008 — Snapshot job exit codes (2026-11-03)

**Context.** The scheduled workflow (task 1.3) must decide, without reading logs, whether to save the
rotated refresh token, commit data, retry later or alert the owner.

**Decision.** `python -m ingestion.mercadolibre.snapshot` exits with:

| Code | Meaning | Token file written? |
|------|---------|---------------------|
| 0 | run finished `success` or `partial` (data written, unless a better snapshot already existed — ADR-007) | yes |
| 1 | run `failed` (no category captured), or aborted after the token was saved (e.g. write error) | yes |
| 2 | re-authorization required (`invalid_grant`, or credentials rejected) | no |
| 3 | new refresh token could not be saved (old one already consumed → re-authorize) | no |
| 4 | token endpoint unavailable after retries (transient); old token probably still valid | no |
| 5 | missing environment variables | no |

**Consequences.** The workflow saves the token only when the file exists (0/1), commits data only on
0/1, retries later on 4, and alerts the owner on 2/3/5. Codes are mirrored in CLAUDE.md.

## ADR-009 — Scheduled snapshot workflow: step conditions and data-commit guard (2026-11-03)

**Context.** `.github/workflows/ml-daily-snapshot.yml` (task 1.3) runs the snapshot daily. The refresh
token is single-use (ADR-004), so a lost rotated token means a manual re-authorization; a commit made
after an interrupted folder swap could delete a good day from the data repo.

**Decision.**
- Only the token-save step runs with `if: always()`: if a token file exists (even after a killed or
  timed-out snapshot step) it is written to `ML_REFRESH_TOKEN` with `gh secret set` via
  `DATA_REPO_PAT` (3 attempts), then shredded. No file = secret untouched (exit 2/3/4/5).
- The snapshot step never fails (`set +e`); its exit code is a step output. The step timeout (40 min)
  is below the job timeout (50 min) so the token step still runs after a hang.
- The data commit runs on `!cancelled()` and only when the snapshot step **itself reported** exit
  0 or 1 (a killed step reports nothing). It also runs if the token save failed: the data is valid.
  It commits only when `snapshot_date=<date>/run.json` exists and **refuses any commit that deletes a
  tracked file** (every day folder has the same five files, so a deletion can only come from an
  interrupted swap; checked with `--no-renames` so a deleted day can't pass as a rename). No changes (ADR-007 kept a better day) = no commit.
- The date is computed once (UTC) and passed with `--date`, so the run, the summary and the commit
  message refer to the same day even across midnight.
- Job-level `concurrency` group keyed by ref, `cancel-in-progress: false`: a running job is never
  cancelled; GitHub keeps one pending job per group (older pending ones are cancelled), and the ref key
  stops a non-main dispatch from displacing a pending main run. The job runs only on `main` (every run
  consumes the token, so branch code must not run it).
- `GITHUB_TOKEN` is `contents: read`; all writes go through `DATA_REPO_PAT` (one PAT, no GitHub
  Environment: owner-accepted risk, ADR-010). Neither checkout persists credentials; the PAT reaches
  git only in the push step, as an env-only `http.extraheader` (never in a URL, argv or `.git/config`).

**Consequences.** If the data contract ever renames or drops a snapshot file, the deletion guard
will refuse the first re-run of an existing day; update the guard in the same PR as the contract.

## ADR-010 — Accepted risks: one PAT, no GitHub Environment (2026-11-03)

**Context.** The review of PRs #4/#5 flagged two hardening gaps:
- A fine-grained PAT applies the same permissions to every repo it selects, so `DATA_REPO_PAT`
  (Contents RW for the data repo + Secrets RW for this repo) also has Contents RW on this **public** repo.
- `workflow_dispatch` runs the workflow file from any branch with repo secrets available, so the
  "main only" guard is a convention, not a control.

**Decision (owner).** Accept both for now: a single PAT, and repo-level secrets without a
GitHub Environment. The owner is the only contributor.

**Consequences.** If the PAT leaked, it could also push to the public repo. Revisit before
adding collaborators or making the project a team effort: split into two PATs (or a GitHub App)
and move the secrets into an Environment restricted to `main` (done for the warehouse secret only, ADR-014,
which also reuses `DATA_REPO_PAT` in a second workflow). Supersedes ADR-003's
"PAT scoped to that repo only" wording.

## ADR-011 — psycopg 3 for PostgreSQL loads; raw Olist as one-transaction COPY (2026-11-03)

**Context.** Task 1.4 loads ~1.55 M rows (nine CSVs) into PostgreSQL. The standard library has no
PostgreSQL driver; shelling out to `psql \copy` would make the load depend on a client binary and
make row-count checks and transactions awkward.

**Decision.**
- Add **`psycopg[binary]` (v3)** as a runtime dependency. It is the maintained PostgreSQL driver,
  exposes `COPY FROM STDIN` as a streaming API (`cursor.copy`) and composes identifiers safely
  (`psycopg.sql`). The `binary` extra bundles libpq, so it installs the same way on the owner's PC,
  GitHub runners and the agents' workspace without a system libpq. It is imported only by loader code.
- Raw tables are all-`text`, loaded with `COPY (FORMAT csv, HEADER)`; the nine tables and their
  audit rows are **one transaction** (truncate + copy + count check per table), so a failure
  leaves the previous complete load in place. Tables are re-created only if their columns drift,
  without `CASCADE`.
- Tests run against a real throwaway PostgreSQL (CI: service container; locally: `DATABASE_URL`
  or an auto-started `initdb` cluster); each session creates and drops its own database.
- Kaggle download uses the documented REST endpoint with httpx (Basic auth from env), not the
  `kaggle` package: one GET does not justify another dependency (and its config-file handling).

**Consequences.** A full real load takes about 3 s locally. The DB role used by tests needs CREATEDB.

## ADR-012 — Enrichment sources: Frankfurter v1 (ECB only) and Nager.Date v4 (2026-11-03; Nager.Date part superseded by ADR-013)

**Context.** Task 1.5 named `api.frankfurter.app/<start>..<end>` and
`date.nager.at/api/v3/PublicHolidays/<year>/<cc>`. Neither API is reachable from the agents'
workspace, so their current shape was checked in the projects' own repositories on GitHub
(`lineofflight/frankfurter` at fb22bf8, 2026-10-30; `nager/Nager.Date` at 657d738, 2026-11-03).
Findings: Frankfurter moved to `api.frankfurter.dev` in 2024; v2 (2026-05) blends 50+ central
banks, while **v1 is frozen, ECB-only, kept "indefinitely"**, and its interval endpoint returns one
key per ECB day. The ECB reference rates do not include ARS. Nager.Date REST **v3 reaches end of life
on 2027-01-31**; v4 (`nagerholidays.com/api/v4/Holidays/{cc}/{year}`) replaces it with different
fields (`nationalHoliday`, `subdivisionCodes`, `holidayTypes`; no `localName`).

**Decision.**
- FX from **Frankfurter v1** (base URL overridable with `--base-url`): the contract promises ECB
  rates, and v1 guarantees the provider. Fetched one calendar year per request, every chunk validated
  and density-checked so a silently downsampled/truncated answer fails. Rates kept as `Decimal`.
- **ARS is not loaded** (not an ECB currency); it is a later task with its own source decision.
- Holidays from **Nager.Date v4**, so the loader does not die in three months.
- No new dependency (httpx + psycopg from ADR-011). Live runs only through the manual workflow
  `enrichment-load.yml` (throwaway Postgres + CSV artifact); no schedule yet.

**Consequences.** The payload shapes are assumptions until the first workflow run; the loaders
fail loudly (exit 1, nothing loaded) on any deviation. If v1 is ever retired, switch to
`/v2/rates?providers=ECB` and update this ADR and DATA_CONTRACT §2b.

## ADR-013 — Nager.Date v3 for now (supersedes the Nager.Date part of ADR-012) (2026-11-03)

**Context.** ADR-012 picked Nager.Date v4 (`nagerholidays.com/api/v4/Holidays/{cc}/{year}`) from the
repository's example docs. Review of PR #8 could not verify that v4 endpoint or its fields. The v3
API is documented in the official README (`date.nager.at/api/v3/PublicHolidays/{year}/{cc}`, fields
`date, localName, name, countryCode, fixed, global, counties, launchYear, types`) and is supported
until 2027-01-31 (official version table).

**Decision (orchestrator).** Use **v3** now. Holidays for the needed years are historical and loaded
once, so the end-of-life date is not a blocker. Validation is strict (required `date`, `name`,
`countryCode` = requested, `global` boolean, non-empty `types`), so any shape change fails loudly.
Migrate to v4 before 2027-01-31 once its API is verified live (PLAN 1.7).

**Consequences.** `raw_enrichment.public_holidays` columns follow v3 (`local_name`, `is_global`,
`counties`, `holiday_types`, `is_fixed`, `launch_year`). The v4 migration will change them, with a
contract update in the same PR. The FX part of ADR-012 still stands.

## ADR-014 — Warehouse on managed PostgreSQL (Neon), transformations in dbt (2026-11-04)

**Context.** Phase 1 loads raw data only into throwaway databases (CI service containers, the manual
enrichment workflow). Phase 2 needs a persistent warehouse that GitHub Actions can write to and the
Phase 2.4 API can read from. The owner reviewed Phase 1 and approved it, with tasks 1.6/1.8 done
alongside Phase 2.

**Decision (owner).**
- **Neon free plan** (managed PostgreSQL 16), one project in an EU region. Limits checked on
  2026-11-04 (neon.com/docs/introduction/free-tier, page updated 2026-10-23; re-confirmed during review): 1 GB storage per project, 100 CU-hours per month,
  compute suspends after 5 min idle.
- **The data repo stays the system of record** (ADR-003 is not reopened): every raw schema in Neon
  (`raw_olist`, `raw_enrichment`, `raw_mercadolibre`) can be dropped and rebuilt from the data repo and
  the public APIs. Raw schemas are owned by the Python loaders; dbt reads them as sources only.
- **dbt** (`dbt-core` + `dbt-postgres`, pinned in `requirements-dbt.txt`, separate from the runtime
  `requirements.txt`) builds exactly three schemas: `staging` (views), `core` (star schema, tables) and
  `marts` (tables); `generate_schema_name` is overridden so dbt never prefixes them. `dbt-postgres`
  brings `psycopg2`; it is used only by dbt (loaders keep psycopg 3, ADR-011). CI runs `dbt build`
  against a throwaway PostgreSQL with small fixtures and **receives no secrets**.
- **Secrets used by the warehouse workflow:**
  - `WAREHOUSE_DATABASE_URL` (direct, non-pooled Neon connection string, `sslmode=require`), stored in
    a GitHub **Environment `warehouse` restricted to `main`**, created by the owner in the GitHub UI.
    Loader steps receive it as `DATABASE_URL`. dbt takes separate profile fields, so the URL is split
    by a Python step that first emits `::add-mask::` for every derived part (password, user, host);
    no shell parsing, no `set -x`. Both the percent-encoded and the decoded password are masked, and the
    parts stay inside one job (`GITHUB_ENV`, never job outputs).
  - `DATA_REPO_PAT` (existing, repo-level) to read the ML snapshots and the Olist archive from the data
    repo. Owner decision: reuse it instead of a new read-only PAT; this extends the ADR-010 accepted
    risk to a second workflow.
- **Olist source:** the owner pushes the Kaggle `archive.zip` once to the private data repo (not
  redistributed; CC BY-NC-SA); the workflow reads it with `--from-zip`.
- **Exposure:** only `marts` (aggregates) are served by the 2.4 API, through a separate read-only
  database role, never the loader credential. Offer-level rows (`core.fact_offer_snapshot`) are not
  exposed, so raw ML data is not republished (ADR-003).

**Consequences.** One more account and one Environment secret (owner). Storage estimate: raw ≈ 0.3 GB
(Olist text tables incl. 1 M geolocation rows), `core`/`marts` add roughly as much again; ML grows by
≈ 2.3 k offers per day (≈ 0.85 M rows per year). Staging as views and a `pg_database_size` line in the
workflow summary keep this visible. 100 CU-hours a month cannot keep a compute awake all month, so the
API must tolerate cold starts and must not keep Neon awake. If limits are hit, the dbt project runs
unchanged on any PostgreSQL; Phase 4 (AWS) may move the warehouse without changing the layout.
