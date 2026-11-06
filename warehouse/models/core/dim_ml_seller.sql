-- Grain: one row per Mercado Libre seller (seller_id) with an offer on an included day (run
-- status success or partial, ADR-019). Real data: 817 rows. Type 1: seller_state = the latest
-- included day's non-NULL state (several on one day: lowest in byte order); NULL if never
-- given. Official stores are an attribute of each offer (core.fact_offer_snapshot), not of the
-- seller. Prefixed dim_ml_ so it never clashes with the Olist dim_seller.

with runs as (

    select * from {{ ref('stg_mercadolibre__runs') }}

),

offers as (

    select * from {{ ref('stg_mercadolibre__offers') }}

),

included_offers as (

    select offers.*
    from offers
    inner join runs
        on runs.snapshot_date = offers.snapshot_date
    where runs.status in {{ ml_included_run_statuses() }}
        and offers.seller_id is not null

),

seller_days as (

    select
        seller_id,
        min(snapshot_date) as first_seen_date,
        max(snapshot_date) as last_seen_date,
        count(distinct snapshot_date)::integer as days_seen
    from included_offers
    group by seller_id

),

latest_state as (

    select
        seller_id,
        seller_state,
        row_number() over (
            partition by seller_id
            order by snapshot_date desc, seller_state collate "C"
        ) as recency
    from included_offers
    where seller_state is not null

),

final as (

    select
        seller_days.seller_id,
        latest_state.seller_state,
        seller_days.first_seen_date,
        seller_days.last_seen_date,
        seller_days.days_seen
    from seller_days
    left join latest_state
        on latest_state.seller_id = seller_days.seller_id
        and latest_state.recency = 1

)

select * from final
