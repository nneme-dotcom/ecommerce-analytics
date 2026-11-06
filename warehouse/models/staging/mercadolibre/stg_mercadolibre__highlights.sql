-- Grain: one row per ranked entry per category per day (snapshot_date, category_id, product_id)
-- = one raw_mercadolibre.highlights record, any type. product_id is a catalog product only when
-- type = 'PRODUCT' (otherwise the id of a USER_PRODUCT or ITEM). No filter here.

with source as (

    select * from {{ source('mercadolibre', 'highlights') }}

),

renamed as (

    select
        snapshot_date,
        run_id,
        line_no,
        captured_at,
        nullif(category_id, '') as category_id,
        nullif(product_id, '') as product_id,
        position,
        nullif(type, '') as type
    from source

)

select * from renamed
