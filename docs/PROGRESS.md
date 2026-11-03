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
