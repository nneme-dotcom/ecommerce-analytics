{#-
    core.fact_order_items: inside the loaded EUR->BRL range (first to last ECB rate_date for
    BRL) every item has an FX rate and EUR amounts (ADR-018). This is what fails if the ECB data
    has a gap longer than the 7-day look-back, or if the carry-forward join breaks.
    Also checks that the EUR columns and the rate are NULL or set together, everywhere.
    Returns the offending items.
-#}

with fx_range as (

    select
        min(rate_date) as first_rate_date,
        max(rate_date) as last_rate_date
    from {{ ref('stg_enrichment__fx_rates') }}
    where base_currency = 'EUR' and quote_currency = 'BRL'

),

items as (

    select * from {{ ref('fact_order_items') }}

)

select
    items.order_id,
    items.order_item_id,
    items.purchase_date,
    items.fx_rate_date,
    items.brl_per_eur,
    items.price_eur
from items
cross join fx_range
where (
        items.purchase_date between fx_range.first_rate_date and fx_range.last_rate_date
        and (items.brl_per_eur is null or items.price_eur is null)
    )
    or (items.fx_rate_date is null) <> (items.brl_per_eur is null)
    or (items.brl_per_eur is null) <> (items.price_eur is null)
    or (items.brl_per_eur is null) <> (items.freight_value_eur is null)
    or (items.brl_per_eur is null) <> (items.item_total_eur is null)
