# warehouse/ — dbt project

PostgreSQL warehouse built with dbt (ADR-014): `raw_*` (Python loaders; read here as **sources
only**) → `staging` (views) → `core` (star schema) → `marts` (metrics). Schemas and models are
defined in DATA_CONTRACT §3, metrics in §4.

```
dbt_project.yml        project config; the folder decides the schema (+schema per folder)
profiles.yml           connection, env vars only (no secrets in the repo)
macros/                generate_schema_name: only staging, core, marts are allowed (parse time);
                       assert_node_schemas: on-run-start re-check of every node's final schema
models/staging/<src>/  _<src>__sources.yml (raw declarations), stg_<src>__<entity>.sql
tests/                 assert_sources_match_contract.sql + generic tests (tests/generic/)
fixtures/              raw data for CI (loaded by the real loaders, ADR-016; see its README)
ci/fixture_db.py       CI helper: load fixtures / check that dbt left raw_* and public alone
```

## Run locally against a throwaway PostgreSQL

Never point this at the Neon warehouse from a workstation or the agents' workspace.

```bash
pip install -r requirements.txt -r requirements-dbt.txt
export DATABASE_URL=postgresql://postgres@localhost:5432/warehouse_ci      # loaders + fixture helper
export DBT_PG_HOST=localhost DBT_PG_PORT=5432 DBT_PG_USER=postgres DBT_PG_DBNAME=warehouse_ci
# export DBT_ENV_SECRET_PG_PASSWORD=...   only if the server needs a password
FIXTURE_DB_ALLOW=1 python -m warehouse.ci.fixture_db load --inventory /tmp/raw_inventory.json
dbt build --project-dir warehouse --profiles-dir warehouse
python -m warehouse.ci.fixture_db check --inventory /tmp/raw_inventory.json
```

`load` replaces raw table contents, so it refuses (exit 2) without `FIXTURE_DB_ALLOW=1` and
whenever a table it writes already has more rows than the fixtures (a real load). The dbt guard
tests (`pytest tests/warehouse`, needs dbt) run dbt on temporary copies of this project.

Profile variables (all optional): `DBT_PG_HOST` (localhost), `DBT_PG_PORT` (5432), `DBT_PG_USER`
(postgres), `DBT_ENV_SECRET_PG_PASSWORD` (empty), `DBT_PG_DBNAME` (ecommerce), `DBT_PG_SSLMODE`
(prefer). The `DBT_ENV_SECRET_` prefix makes dbt scrub the password from its logs.

## Conventions

- One model per file. Staging models are named `stg_<source>__<entity>`; `core` uses `dim_<entity>`
  and `fact_<process>` (as `core.fact_offer_snapshot` in ADR-014). The first line comment
  states the **grain**.
- CTEs (`source` → `renamed` → ...) and window functions over nested subqueries.
- Every model has data tests: grain uniqueness (`unique` or `unique_combination_of_columns`),
  not-null keys, `relationships`, `accepted_values`.
- A new raw table is declared in its `_<src>__sources.yml` with **all** its columns and types;
  `assert_sources_match_contract` fails if the declaration and the database differ.
- dbt never writes to `raw_*` (no seeds into raw, no hooks on raw); the CI check enforces it.
