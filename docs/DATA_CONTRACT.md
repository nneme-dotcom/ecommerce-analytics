# Data contract

Single source of truth for every schema shared between agents. Change it **before** (or in the same PR as) the code.
Status per section: `draft` (may change freely) · `stable` (changes need a DECISIONS entry).

---

## 1. Mercado Libre raw snapshots — `draft`

One snapshot run per UTC day. A run is identified by `snapshot_date` (YYYY-MM-DD) and `run_id` (UUID).
Format: newline-delimited JSON (`.jsonl`), gzip-compressed. Layout (relative to the storage root, see ADR-003):

```
mercadolibre/snapshot_date=YYYY-MM-DD/
  run.json                  run metadata
  highlights.jsonl.gz       one line per (category, ranked product)
  products.jsonl.gz         one line per catalog product seen in the run
  offers.jsonl.gz           one line per offer per product
  trends.jsonl.gz           one line per trending keyword
```

Every line carries `snapshot_date`, `run_id` and `captured_at` (UTC ISO-8601).

### run.json
| field | type | notes |
|-------|------|-------|
| run_id | string (uuid) | |
| snapshot_date | date | |
| started_at / finished_at | timestamp UTC | |
| site_id | string | `MLA` |
| categories | string[] | category ids captured |
| requests_made | int | |
| errors | object[] | `{path, status, count}` — no tokens, no payloads |
| status | string | `success` · `partial` · `failed` |

### highlights
| field | type | source |
|-------|------|--------|
| category_id | string | request param |
| position | int | `content[].position` |
| product_id | string | `content[].id` (only `type == PRODUCT`) |

### products
| field | type | source |
|-------|------|--------|
| product_id | string | `id` |
| name | string | `name` |
| domain_id | string | `domain_id` |
| family_name | string? | `family_name` |
| status | string | `status` |
| brand | string? | `attributes[id=BRAND].value_name` |
| model | string? | `attributes[id=MODEL].value_name` |
| permalink | string | `permalink` |

### offers
| field | type | source (`/products/{id}/items` → `results[]`) |
|-------|------|--------|
| product_id | string | request param |
| item_id | string | `item_id` |
| seller_id | int | `seller_id` |
| official_store_id | int? | `official_store_id` |
| price | decimal | `price` |
| original_price | decimal? | `original_price` |
| currency_id | string | `currency_id` |
| condition | string | `condition` |
| listing_type_id | string | `listing_type_id` |
| category_id | string | `category_id` |
| free_shipping | bool | `shipping.free_shipping` |
| logistic_type | string? | `shipping.logistic_type` |
| shipping_mode | string? | `shipping.mode` |
| deal_ids | string[] | `deal_ids` |
| tags | string[] | `tags` |
| seller_state | string? | `seller_address.state.name` |

### trends
| field | type | source |
|-------|------|--------|
| position | int | index in response (1-based) |
| keyword | string | `keyword` |
| url | string | `url` |

### Initial categories (`MLA`)
To be confirmed by task 1.2 against `/sites/MLA/categories`:
Cell phones `MLA1055` + four more chosen for variety (e.g. notebooks, TVs, sneakers, small appliances).

---

## 2. Olist raw — `draft`

The nine CSVs of the public dataset, loaded unchanged into schema `raw_olist`:
customers, geolocation, order_items, order_payments, order_reviews, orders, products, sellers,
product_category_name_translation.

---

## 3. Warehouse (PostgreSQL) — `draft`

Layers: `raw_*` (as ingested) → `staging` (typed, cleaned, renamed) → `core` (star schema) → `marts` (metrics).
Detailed tables are defined in tasks 2.1–2.3.

---

## 4. Metric definitions — `draft`

Each metric served by the API is defined here (name, formula, grain, filters) in task 2.3.
