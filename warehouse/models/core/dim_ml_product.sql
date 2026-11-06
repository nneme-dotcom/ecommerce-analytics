-- Grain: one row per Mercado Libre catalog product (product_id) seen on an included day
-- (run status success or partial, ADR-019) in products, offers or a PRODUCT highlight. Real
-- data: 112 rows. Type 1: the attributes are the whole products row of the latest included day
-- that has one (attributes_as_of); NULL with has_product_details = false if the product was
-- never fetched (e.g. its detail call failed on a partial day but its offers were captured).
-- Prefixed dim_ml_ so it never clashes with the Olist dim_product.

with runs as (

    select * from {{ ref('stg_mercadolibre__runs') }}

),

products as (

    select * from {{ ref('stg_mercadolibre__products') }}

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

-- Every (day, product) sighting, tagged by where it was seen.
sightings as (

    select snapshot_date, product_id, 'products' as seen_in
    from products
    union all
    select distinct snapshot_date, product_id, 'offers' as seen_in
    from offers
    union all
    select snapshot_date, product_id, 'highlights' as seen_in
    from highlights
    where type = 'PRODUCT'

),

included_sightings as (

    select sightings.*
    from sightings
    inner join included_days
        on included_days.snapshot_date = sightings.snapshot_date

),

product_days as (

    select
        product_id,
        min(snapshot_date) as first_seen_date,
        max(snapshot_date) as last_seen_date,
        (count(distinct snapshot_date) filter (where seen_in = 'highlights'))::integer
            as days_ranked,
        (count(distinct snapshot_date) filter (where seen_in = 'offers'))::integer
            as days_with_offers
    from included_sightings
    group by product_id

),

latest_details as (

    select
        products.*,
        row_number() over (
            partition by products.product_id
            order by products.snapshot_date desc
        ) as recency
    from products
    inner join included_days
        on included_days.snapshot_date = products.snapshot_date

),

final as (

    select
        product_days.product_id,
        latest_details.name as product_name,
        latest_details.domain_id,
        latest_details.family_name,
        latest_details.status as product_status,
        latest_details.brand,
        latest_details.model,
        latest_details.permalink,
        latest_details.product_id is not null as has_product_details,
        latest_details.snapshot_date as attributes_as_of,
        product_days.first_seen_date,
        product_days.last_seen_date,
        product_days.days_ranked,
        product_days.days_with_offers
    from product_days
    left join latest_details
        on latest_details.product_id = product_days.product_id
        and latest_details.recency = 1

)

select * from final
