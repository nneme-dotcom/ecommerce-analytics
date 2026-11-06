-- Grain: one row per trending keyword per day (snapshot_date, position) = one
-- raw_mercadolibre.trends record. Not used by core yet (no price meaning); kept for marts.

with source as (

    select * from {{ source('mercadolibre', 'trends') }}

),

renamed as (

    select
        snapshot_date,
        run_id,
        line_no,
        captured_at,
        position,
        nullif(keyword, '') as keyword,
        nullif(url, '') as url
    from source

)

select * from renamed
