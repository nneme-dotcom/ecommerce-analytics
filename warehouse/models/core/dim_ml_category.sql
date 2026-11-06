-- Grain: one row per Mercado Libre category (category_id) seen on an included day (run status
-- success or partial, ADR-019) as a best-seller category (highlights, any type) or as an
-- offer's own category_id (usually a leaf below the ranked category). Real data: 19 rows.
-- No category name: the snapshot does not call /categories/{id}, and none is invented here.

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

    select snapshot_date
    from runs
    where status in {{ ml_included_run_statuses() }}

),

sightings as (

    select distinct snapshot_date, category_id, 'ranking' as role
    from highlights
    union all
    select distinct snapshot_date, category_id, 'listing' as role
    from offers
    where category_id is not null

),

included_sightings as (

    select sightings.*
    from sightings
    inner join included_days
        on included_days.snapshot_date = sightings.snapshot_date

),

final as (

    select
        category_id,
        bool_or(role = 'ranking') as is_ranking_category,
        bool_or(role = 'listing') as is_listing_category,
        min(snapshot_date) as first_seen_date,
        max(snapshot_date) as last_seen_date,
        (count(distinct snapshot_date) filter (where role = 'ranking'))::integer as days_ranked
    from included_sightings
    group by category_id

)

select * from final
