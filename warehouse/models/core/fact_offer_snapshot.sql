-- Grain: one row per offer (item_id listed on catalog product_id) per snapshot_date, for the
-- days whose run status is success or partial (ADR-007, ADR-019); failed days are left out.
-- Real data: 9,439 rows over 4 days (= raw offers of those days). Offers without an item_id are
-- left out (they cannot be followed across days; warned in staging).
-- Prices stay in the offer's currency_id (ARS): there is no ECB ARS rate (ADR-012).
-- Discount only where original_price > price. Best-seller columns = the product's best
-- (lowest) position that day across the categories ranking it; the position is a demand proxy,
-- not sales. Offer-level third-party data: never exposed by the API (ADR-003, ADR-014).

with runs as (

    select * from {{ ref('stg_mercadolibre__runs') }}

),

offers as (

    select * from {{ ref('stg_mercadolibre__offers') }}

),

highlights as (

    select * from {{ ref('stg_mercadolibre__highlights') }}

),

included_days as (

    select
        snapshot_date,
        status as run_status
    from runs
    where status in {{ ml_included_run_statuses() }}

),

-- One row per (day, catalog product) ranked in at least one category: the best position wins,
-- ties by category_id in byte order (deterministic, independent of the collation).
ranked_products as (

    select
        highlights.snapshot_date,
        highlights.product_id,
        highlights.category_id,
        highlights.position,
        count(*) over (
            partition by highlights.snapshot_date, highlights.product_id
        ) as category_count,
        row_number() over (
            partition by highlights.snapshot_date, highlights.product_id
            order by highlights.position nulls last, highlights.category_id collate "C"
        ) as best_rank
    from highlights
    inner join included_days
        on included_days.snapshot_date = highlights.snapshot_date
    where highlights.type = 'PRODUCT'

),

best_seller as (

    select
        snapshot_date,
        product_id,
        category_id as best_seller_category_id,
        position as best_seller_position,
        category_count::integer as best_seller_category_count
    from ranked_products
    where best_rank = 1

),

included_offers as (

    select
        offers.*,
        included_days.run_status
    from offers
    inner join included_days
        on included_days.snapshot_date = offers.snapshot_date
    where offers.item_id is not null

),

joined as (

    select
        included_offers.snapshot_date,
        included_offers.product_id,
        included_offers.item_id,
        included_offers.seller_id,
        included_offers.category_id as listing_category_id,
        included_offers.run_status,
        included_offers.captured_at,
        included_offers.price,
        included_offers.original_price,
        included_offers.currency_id,
        coalesce(included_offers.original_price > included_offers.price, false) as has_discount,
        included_offers.condition,
        included_offers.listing_type_id,
        included_offers.official_store_id,
        included_offers.official_store_id is not null as is_official_store,
        included_offers.free_shipping,
        included_offers.logistic_type,
        included_offers.shipping_mode,
        included_offers.logistic_type = 'fulfillment' as is_fulfillment,
        cardinality(included_offers.deal_ids) > 0 as has_deal,
        best_seller.product_id is not null as is_best_seller,
        best_seller.best_seller_category_id,
        best_seller.best_seller_position,
        coalesce(best_seller.best_seller_category_count, 0) as best_seller_category_count
    -- Left join: an offer of a product that was not ranked that day keeps its row (NULL rank).
    from included_offers
    left join best_seller
        on best_seller.snapshot_date = included_offers.snapshot_date
        and best_seller.product_id = included_offers.product_id

),

final as (

    select
        snapshot_date,
        product_id,
        item_id,
        seller_id,
        listing_category_id,
        run_status,
        captured_at,
        price,
        original_price,
        currency_id,
        has_discount,
        case when has_discount then original_price - price end as discount_amount,
        case
            when has_discount then round((original_price - price) / original_price, 4)
        end as discount_pct,
        condition,
        listing_type_id,
        official_store_id,
        is_official_store,
        free_shipping,
        logistic_type,
        shipping_mode,
        is_fulfillment,
        has_deal,
        is_best_seller,
        best_seller_category_id,
        best_seller_position,
        best_seller_category_count,
        count(*) over (partition by snapshot_date, product_id)::integer as offers_in_product,
        rank() over (
            partition by snapshot_date, product_id, currency_id
            order by price
        )::integer as price_rank_in_product
    from joined

)

select * from final
