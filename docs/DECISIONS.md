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
