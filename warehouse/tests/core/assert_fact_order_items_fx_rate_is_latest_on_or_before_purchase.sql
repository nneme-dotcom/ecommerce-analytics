{#-
    core.fact_order_items FX rule (ADR-018): when an item has a rate, fx_rate_date is on or before
    the purchase date, at most 7 days before it, no BRL rate was published between the two, and
    brl_per_eur is the rate published on fx_rate_date. When an item has no rate, no BRL rate
    exists in the 7 days up to its purchase date. Returns the offending items with the reason.
-#}

with brl_rates as (

    select rate_date, rate
    from {{ ref('stg_enrichment__fx_rates') }}
    where base_currency = 'EUR' and quote_currency = 'BRL'

),

items as (

    select * from {{ ref('fact_order_items') }}

),

-- The latest BRL rate inside each item's look-back window (purchase_date - 7 .. purchase_date).
latest_in_window as (

    select
        items.order_id,
        items.order_item_id,
        max(brl_rates.rate_date) as expected_rate_date
    from items
    inner join brl_rates
        on brl_rates.rate_date between items.purchase_date - 7 and items.purchase_date
    group by items.order_id, items.order_item_id

),

compared as (

    select
        items.order_id,
        items.order_item_id,
        items.purchase_date,
        items.fx_rate_date,
        latest_in_window.expected_rate_date,
        items.brl_per_eur,
        published.rate as published_rate,
        case
            when items.fx_rate_date is distinct from latest_in_window.expected_rate_date
                then 'fx_rate_date is not the latest rate in the 7-day look-back'
            when items.fx_rate_date is not null and items.brl_per_eur is distinct from published.rate
                then 'brl_per_eur differs from the rate published on fx_rate_date'
        end as problem
    from items
    left join latest_in_window
        on latest_in_window.order_id = items.order_id
        and latest_in_window.order_item_id = items.order_item_id
    left join brl_rates as published
        on published.rate_date = items.fx_rate_date

)

select *
from compared
where problem is not null
