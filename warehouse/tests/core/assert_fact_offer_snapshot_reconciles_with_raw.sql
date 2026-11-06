{#-
    Row-count reconciliation of core.fact_offer_snapshot with raw (DATA_CONTRACT §3, ADR-019),
    per snapshot_date: the fact has exactly the raw_mercadolibre.offers rows that have an
    item_id, a seller_id, a price and a currency_id (empty text counts as missing, as in
    staging) on every day whose run status is included (success, partial), and no row on any
    other day (failed, or a day missing from runs). Read from the source, not from staging, so
    a filter added in between cannot hide rows. Returns one row per day whose counts differ.
-#}

with raw_offers as (

    select
        offers.snapshot_date,
        count(*) as raw_rows
    from {{ source('mercadolibre', 'offers') }} as offers
    inner join {{ source('mercadolibre', 'runs') }} as runs
        on runs.snapshot_date = offers.snapshot_date
    where runs.status in {{ ml_included_run_statuses() }}
        and nullif(offers.item_id, '') is not null
        and offers.seller_id is not null
        and offers.price is not null
        and nullif(offers.currency_id, '') is not null
    group by offers.snapshot_date

),

fact_rows as (

    select
        snapshot_date,
        count(*) as fact_rows
    from {{ ref('fact_offer_snapshot') }}
    group by snapshot_date

)

select
    coalesce(raw_offers.snapshot_date, fact_rows.snapshot_date) as snapshot_date,
    coalesce(raw_offers.raw_rows, 0) as raw_rows,
    coalesce(fact_rows.fact_rows, 0) as fact_rows
from raw_offers
full outer join fact_rows
    on fact_rows.snapshot_date = raw_offers.snapshot_date
where coalesce(raw_offers.raw_rows, 0) <> coalesce(fact_rows.fact_rows, 0)
