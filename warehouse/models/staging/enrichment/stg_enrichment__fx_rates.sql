-- Grain: one row per ECB publication day and quote currency (rate_date, quote_currency); the
-- base is always EUR. No rows on weekends and TARGET holidays: carrying a rate forward is a
-- core decision (fact_order_items, ADR-018), not done here. Already typed in raw (§2b); this
-- view only selects the columns used downstream and drops the constant `source`.

with source as (

    select * from {{ source('enrichment', 'fx_rates') }}

),

renamed as (

    select
        rate_date,
        base_currency::text as base_currency,
        quote_currency::text as quote_currency,
        rate
    from source

)

select * from renamed
