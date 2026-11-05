{#-
    core.fact_order_items FX rule (ADR-018): when an item has a rate, fx_rate_date is on or before
    the purchase date, at most 7 days before it, no BRL rate was published between the two, and
    brl_per_eur is the rate published on fx_rate_date. When an item has no rate, no BRL rate
    exists in the 7 days up to its purchase date. Returns the offending items with the reason.

    Cost: the expected rate date depends only on the purchase date, so it is computed once per
    distinct purchase_date (real data: ~630 days x ~600 rates) and then joined to the items on
    equality. Joining every item to the rate range directly is a nested loop over
    112,650 items x all rates (10.5 s on the real data, review round 1).
-#}

with brl_rates as (

    select rate_date, rate
    from {{ ref('stg_enrichment__fx_rates') }}
    where base_currency = 'EUR' and quote_currency = 'BRL'

),

items as (

    select * from {{ ref('fact_order_items') }}

),

purchase_days as (

    select distinct purchase_date
    from items
    where purchase_date is not null

),

-- The latest BRL rate inside each purchase day's look-back window (day - 7 .. day);
-- NULL when the window holds no rate.
expected_by_day as (

    select
        purchase_days.purchase_date,
        max(brl_rates.rate_date) as expected_rate_date
    from purchase_days
    left join brl_rates
        on brl_rates.rate_date between purchase_days.purchase_date - 7 and purchase_days.purchase_date
    group by purchase_days.purchase_date

),

compared as (

    select
        items.order_id,
        items.order_item_id,
        items.purchase_date,
        items.fx_rate_date,
        expected_by_day.expected_rate_date,
        items.brl_per_eur,
        published.rate as published_rate,
        case
            when items.fx_rate_date is distinct from expected_by_day.expected_rate_date
                then 'fx_rate_date is not the latest rate in the 7-day look-back'
            when items.fx_rate_date is not null and items.brl_per_eur is distinct from published.rate
                then 'brl_per_eur differs from the rate published on fx_rate_date'
        end as problem
    from items
    left join expected_by_day
        on expected_by_day.purchase_date = items.purchase_date
    left join brl_rates as published
        on published.rate_date = items.fx_rate_date

)

select *
from compared
where problem is not null
