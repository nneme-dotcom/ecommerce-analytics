# Mercado Libre API probe — 2026-11-03

Manual feasibility check run from the owner's machine with the app `ecommerce-nadir`, site `MLA` (Argentina).

## Auth

- Flow: authorization code **with PKCE (S256)**. Without `code_verifier` the token endpoint returns
  `400 invalid_request: code_verifier is a required parameter`.
- Access token TTL: 21600 s (6 h). Refresh token issued (`offline_access`).
- App permissions set to **read** for: Users, Communications, Publication & sync, Product advertising, Business metrics.
  With only Users + Communications, every catalog endpoint returned 403.

## Endpoint results (with token)

| Endpoint | Status | Notes |
|----------|--------|-------|
| `/users/me` | 200 | |
| `/sites/MLA/categories` | 200 | Needs publish-sync/metrics read scopes |
| `/categories/{id}` | 200 | Also works without token |
| `/highlights/MLA/category/{cat}` | 200 | `content[]`: `{id, position, type: PRODUCT}` |
| `/products/search?site_id=MLA&q=...&status=active` | 200 | Catalog search |
| `/products/{id}` | 200 | name, domain_id, attributes, pictures, buy_box_winner (may be null) |
| `/products/{id}/items` | 200 | All offers for a catalog product (example: 42 offers) |
| `/trends/MLA` | 200 | Trending search keywords |
| `/sites/MLA/listing_types`, `/currencies/ARS` | 200 | |
| `/sites/MLA/search` | **403** | Blocked with and without token |
| `/items/{id}`, `/items/{id}/prices` | **403** | Only the seller's own items |

## Offer fields (`/products/{id}/items` → `results[]`)

`item_id, site_id, seller_id, accepts_mercadopago, price, category_id, currency_id, warranty,
condition, listing_type_id, international_delivery_mode, tier, inventory_id, tags, deal_ids,
official_store_id, original_price, shipping{cost, free_shipping, local_pick_up, logistic_type, mode,
store_pick_up, tags}, seller_address, sale_terms, user_product_id, min_purchase_unit`

Example: product `MLA58353028` (Motorola Moto G06 128GB), rank #1 in Cell Phones (`MLA1055`):
price 255,999.36 ARS vs original 399,999 ARS, free shipping, `logistic_type=cross_docking`.

## Not available

Units sold / orders of other sellers. Ranking position is the demand proxy.
