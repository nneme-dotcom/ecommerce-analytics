# Warehouse fixtures (synthetic)

Hand-made, **synthetic** rows (not real Olist, ECB or Nager.Date data) for the CI `dbt build`
(ADR-016). `python -m warehouse.ci.fixture_db load` loads them into a throwaway PostgreSQL with the
real loaders, so the raw tables have exactly the DDL of DATA_CONTRACT §2 and §2b:

| folder / file | loaded by | into |
|---------------|-----------|------|
| `raw_olist/*.csv` (the nine Olist files, contract headers) | `ingestion.olist.load` (`prepare` + `load`) | `raw_olist.*` |
| `raw_enrichment/fx_rates_frankfurter_v1.json` (a Frankfurter v1 interval response + its request) | `ingestion.enrichment.fx_rates` (`parse_rates` + `load_rates`) | `raw_enrichment.fx_rates` |
| `raw_enrichment/public_holidays_nager_v3.json` (Nager.Date v3 responses keyed `CC:YYYY`) | `ingestion.enrichment.holidays` (`parse_holidays` + `load_holidays`) | `raw_enrichment.public_holidays` |

The Olist rows are referentially consistent so models can be tested end to end: 3 orders
(two delivered, one on time and one late; one canceled), 3 customers for 2 unique customers,
2 sellers, 4 products (one with no category), payments that add up to price + freight per order,
a review with a line break, and a zip prefix with two geolocation rows.

These fixtures are independent of the loader-test fixtures in `tests/olist/fixtures/`. Extend
them when a model needs a new case, and keep the expectations in the model tests in sync.
