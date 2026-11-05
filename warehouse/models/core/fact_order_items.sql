-- Grain: one row per order item (order_id, order_item_id), every raw_olist.order_items record
-- (real data: 112,650 rows; orders without items have no row here but are in fact_deliveries).
-- Amounts in BRL as published by Olist, plus EUR at the ECB rate (ADR-018):
--   brl_per_eur = the latest ECB EUR->BRL rate published on or before the purchase date, looked
--   back at most 7 calendar days (ECB publication gaps are weekends and TARGET holidays, at most
--   4 days); fx_rate_date says which one. The rate and EUR columns are NULL before the first
--   rate, or when no rate exists on or within 7 days before the purchase date (so the last rate
--   is carried at most 7 days past the end of the loaded range). EUR = BRL / brl_per_eur,
--   rounded to 4 decimals.

with order_items as (

    select * from {{ ref('stg_olist__order_items') }}

),

orders as (

    select * from {{ ref('stg_olist__orders') }}

),

customers as (

    select * from {{ ref('stg_olist__customers') }}

),

fx_rates as (

    select * from {{ ref('stg_enrichment__fx_rates') }}

),

brl_rates as (

    select
        rate_date,
        rate as brl_per_eur
    from fx_rates
    where base_currency = 'EUR'
        and quote_currency = 'BRL'

),

-- Each published rate is valid from its own date until the day before the next publication,
-- and for at most 7 days (the look-back cap).
brl_rate_validity as (

    select
        rate_date,
        brl_per_eur,
        least(
            coalesce(lead(rate_date) over (order by rate_date) - 1, rate_date + 7),
            rate_date + 7
        ) as valid_to
    from brl_rates

),

-- One row per calendar day covered by a rate, so the fact joins on equality.
brl_rate_days as (

    select
        day::date as rate_day,
        brl_rate_validity.rate_date as fx_rate_date,
        brl_rate_validity.brl_per_eur
    from brl_rate_validity
    cross join generate_series(
        brl_rate_validity.rate_date, brl_rate_validity.valid_to, interval '1 day'
    ) as day

),

joined as (

    select
        order_items.order_id,
        order_items.order_item_id,
        order_items.product_id,
        order_items.seller_id,
        customers.customer_unique_id,
        orders.customer_id,
        customers.customer_zip_code_prefix,
        orders.order_status,
        orders.order_purchase_timestamp as order_purchased_at,
        orders.order_purchase_timestamp::date as purchase_date,
        order_items.shipping_limit_date as shipping_limit_at,
        order_items.price as price_brl,
        order_items.freight_value as freight_value_brl,
        order_items.price + order_items.freight_value as item_total_brl,
        brl_rate_days.fx_rate_date,
        brl_rate_days.brl_per_eur
    -- Left joins: an item whose order or customer is missing keeps its row (the row-count
    -- reconciliation holds) and fails the not_null / relationships tests instead of vanishing.
    from order_items
    left join orders
        on orders.order_id = order_items.order_id
    left join customers
        on customers.customer_id = orders.customer_id
    left join brl_rate_days
        on brl_rate_days.rate_day = orders.order_purchase_timestamp::date

),

final as (

    select
        order_id,
        order_item_id,
        product_id,
        seller_id,
        customer_unique_id,
        customer_id,
        customer_zip_code_prefix,
        order_status,
        order_purchased_at,
        purchase_date,
        shipping_limit_at,
        price_brl,
        freight_value_brl,
        item_total_brl,
        fx_rate_date,
        brl_per_eur,
        round(price_brl / brl_per_eur, 4) as price_eur,
        round(freight_value_brl / brl_per_eur, 4) as freight_value_eur,
        round(item_total_brl / brl_per_eur, 4) as item_total_eur
    from joined

)

select * from final
