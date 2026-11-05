# Warehouse fixtures

Raw data for the CI `dbt build` (ADR-016), of two kinds:

- **Transactional rows are synthetic**: the orders, customers, sellers, products, order items,
  payments, reviews and geolocation rows are hand-made (made-up ids, dates and amounts), not
  copied from the Olist dataset.
- **Reference values are real public facts**: the 3 product category translations (Olist's
  Portuguese name and its English translation), the public holidays (Nager.Date) and the
  EUR-based FX rates (ECB reference rates via Frankfurter) are the published values for their
  dates, in the APIs' response shape.

`FIXTURE_DB_ALLOW=1 python -m warehouse.ci.fixture_db load` loads them into a throwaway
PostgreSQL with the real loaders, so the raw tables have exactly the DDL of DATA_CONTRACT §2 and §2b:

| folder / file | loaded by | into |
|---------------|-----------|------|
| `raw_olist/*.csv` (the nine Olist files, contract headers) | `ingestion.olist.load` (`prepare` + `load`) | `raw_olist.*` |
| `raw_enrichment/fx_rates_frankfurter_v1.json` (a Frankfurter v1 interval response + its request) | `ingestion.enrichment.fx_rates` (`parse_rates` + `load_rates`) | `raw_enrichment.fx_rates` |
| `raw_enrichment/public_holidays_nager_v3.json` (Nager.Date v3 responses keyed `CC:YYYY`) | `ingestion.enrichment.holidays` (`parse_holidays` + `load_holidays`) | `raw_enrichment.public_holidays` |

The Olist rows are referentially consistent so models can be tested end to end, and each one
exists for a case the models must handle (task 2.1, DATA_CONTRACT §3, ADR-018):

| case | rows |
|------|------|
| delivered early / late / on the estimated day (a timestamp after 00:00 of that day) | o1 / o2 / o7 |
| non-delivered orders: canceled, shipped, created (no approval, no items), invoiced, processing | o3, o6 / o4 / o8 / o9 / o10 |
| `delivered` status without a customer delivery date; `canceled` with one (both: no metrics) | o5; o6 |
| repeat customer (two `customer_id`s, one `customer_unique_id`) who moved between orders | u2 = c2 (20040), c7 (22041) |
| repeat customer at the same address | u1 = c1, c3 |
| 4-digit zip prefix padded to 5 digits by staging | c8 `1310` → `01310` |
| prefix with several geolocation rows: exact duplicate, accent variant, a point outside Brazil | 01310 (4 rows; median keeps it in São Paulo) |
| prefix whose (city, state) pairs tie | 22041 (`copacabana` wins in byte order) |
| prefix with no geolocation row, only from a customer / a seller | 69900 / 88015 |
| NULL category; a category with no translation (as `pc_gamer` in the real data) | p4; p5 |
| a product measure written `1450.0` | p5 |
| an order with two units of the same product; an order with no items | o7; o8 |
| duplicate `review_id` on two orders; an order with two reviews; a quoted empty title (`""`) | r1 (o1, o6); o7; r4 |
| FX: purchase on a publication day / Saturday (carry-forward) / 7 and 8 days after the last rate / before the first rate / outside the range | o1, o5 / o4 / o9, o10 / o6 / o2, o3, o7 |
| holidays: nationwide public (flagged), regional optional (not flagged), year without BR calendar (NULL) | 2017-10-12, 2017-11-02 / 2017-11-20 / 2018 |

Payments add up to price + freight per order (o8: one `not_defined` payment of 0.00). The exact
expected outputs of the core rules are in the dbt unit tests
(`models/core/_core__unit_tests.yml`), which use their own inline rows.

These fixtures are independent of the loader-test fixtures in `tests/olist/fixtures/`. Extend
them when a model needs a new case, and keep the expectations in the model tests in sync.
