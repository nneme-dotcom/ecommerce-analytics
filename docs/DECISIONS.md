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
