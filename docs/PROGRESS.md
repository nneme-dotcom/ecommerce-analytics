# Progress

Current state at the top; handoff log below (newest first).

## Current state

- **Phase:** 0 — Foundations
- **Next up:** 0.4 (decide raw snapshot storage) → 1.1 / 1.2 / 1.3 (start the ML history ASAP)
- **Blockers:** owner decision on ADR-003 storage; owner must rotate the ML client secret before it is stored in GitHub secrets.

## Handoff template

```
### YYYY-MM-DD — <agent role> — <task #>
Done:
Not done / open:
Gotchas for the next agent:
Files touched:
```

## Log

### 2026-11-03 — orchestrator — 0.1, 0.2, 0.3
Done: repo scaffold, project memory docs, agent role definitions, CI workflow.
Verified the Mercado Libre API by hand (see docs/API_PROBE.md): auth works with PKCE; catalog,
best sellers, offers per product and trends work; general search and item detail are blocked.
Not done / open: ADR-003 storage choice.
Gotchas: the ML app enforces PKCE; refresh tokens rotate. Nothing that calls the ML API can be run
from the agents' cloud workspace — use mocks/fixtures.
