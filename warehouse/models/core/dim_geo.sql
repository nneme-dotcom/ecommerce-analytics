-- Grain: one row per 5-digit zip code prefix (zip_code_prefix) seen in raw_olist.geolocation,
-- customers or sellers (real data: ~19.2k rows from 1,000,163 geolocation rows).
-- Deterministic aggregation rule (ADR-018):
--   latitude / longitude = median (percentile_cont 0.5) of the prefix's geolocation points,
--     computed separately per axis; robust to the few points far outside Brazil.
--   city / state = the most frequent (city, state) pair among the prefix's geolocation rows;
--     ties -> lowest state, then lowest city, in byte order (COLLATE "C", so the result does not
--     depend on the database's collation).
--   Prefixes with no geolocation row (some customers and sellers): city / state = the most
--     frequent pair among those customers' and sellers' rows (same tie rule), lat/lng NULL,
--     has_geolocation = false.

with geolocation as (

    select * from {{ ref('stg_olist__geolocation') }}

),

customers as (

    select * from {{ ref('stg_olist__customers') }}

),

sellers as (

    select * from {{ ref('stg_olist__sellers') }}

),

geo_points as (

    select
        geolocation_zip_code_prefix as zip_code_prefix,
        count(*) as geolocation_rows,
        percentile_cont(0.5) within group (order by geolocation_lat) as latitude,
        percentile_cont(0.5) within group (order by geolocation_lng) as longitude
    from geolocation
    group by geolocation_zip_code_prefix

),

geo_places as (

    select
        geolocation_zip_code_prefix as zip_code_prefix,
        geolocation_city as city,
        geolocation_state as state,
        count(*) as n_rows
    from geolocation
    group by geolocation_zip_code_prefix, geolocation_city, geolocation_state

),

party_rows as (

    select
        customer_zip_code_prefix as zip_code_prefix,
        customer_city as city,
        customer_state as state
    from customers

    union all

    select
        seller_zip_code_prefix as zip_code_prefix,
        seller_city as city,
        seller_state as state
    from sellers

),

party_places as (

    select
        zip_code_prefix,
        city,
        state,
        count(*) as n_rows
    from party_rows
    where zip_code_prefix is not null
    group by zip_code_prefix, city, state

),

-- Geolocation candidates rank before customer/seller candidates (source_rank), so the
-- customer/seller rows only name prefixes that geolocation does not cover.
candidate_places as (

    select zip_code_prefix, city, state, n_rows, 1 as source_rank from geo_places
    union all
    select zip_code_prefix, city, state, n_rows, 2 as source_rank from party_places

),

ranked_places as (

    select
        zip_code_prefix,
        city,
        state,
        row_number() over (
            partition by zip_code_prefix
            order by
                source_rank,
                n_rows desc,
                state collate "C",
                city collate "C"
        ) as place_rank
    from candidate_places
    where zip_code_prefix is not null

),

final as (

    select
        ranked_places.zip_code_prefix,
        ranked_places.city,
        ranked_places.state,
        geo_points.latitude,
        geo_points.longitude,
        coalesce(geo_points.geolocation_rows, 0) as geolocation_rows,
        geo_points.zip_code_prefix is not null as has_geolocation
    from ranked_places
    left join geo_points
        on geo_points.zip_code_prefix = ranked_places.zip_code_prefix
    where ranked_places.place_rank = 1

)

select * from final
