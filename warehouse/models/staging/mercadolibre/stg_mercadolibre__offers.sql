-- Grain: one row per offer per product per day (snapshot_date, product_id, item_id) = one
-- raw_mercadolibre.offers record. item_id is nullable in raw (the writer keeps an offer without
-- one); such rows stay here and are left out of core.fact_offer_snapshot. Prices are in the
-- offer's currency_id, exact as published.

with source as (

    select * from {{ source('mercadolibre', 'offers') }}

),

renamed as (

    select
        snapshot_date,
        run_id,
        line_no,
        captured_at,
        nullif(product_id, '') as product_id,
        nullif(item_id, '') as item_id,
        seller_id,
        official_store_id,
        price,
        original_price,
        nullif(currency_id, '') as currency_id,
        nullif(condition, '') as condition,
        nullif(listing_type_id, '') as listing_type_id,
        nullif(category_id, '') as category_id,
        free_shipping,
        nullif(logistic_type, '') as logistic_type,
        nullif(shipping_mode, '') as shipping_mode,
        deal_ids,
        tags,
        nullif(seller_state, '') as seller_state
    from source

)

select * from renamed
