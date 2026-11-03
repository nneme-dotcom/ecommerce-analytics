# Progress

Current state at the top; handoff log below (newest first).

## Current state

- **Phase:** 0 — Foundations
- **Next up:** 1.1 / 1.2 / 1.3 (start the ML history ASAP)
- **Blockers:** owner must rotate the ML client secret and create a fine-grained PAT before the scheduled job can run.

## Handoff template

```
### YYYY-MM-DD — <agent role> — <task #>
Done:
Not done / open:
Gotchas for the next agent:
Files touched:
```

## Log

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
