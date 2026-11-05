-- Grain: one row per seller (seller_id). Real data: 3,095 rows. Natural key from Olist.
-- seller_zip_code_prefix joins core.dim_geo for coordinates; city/state are as the seller gave them.

with sellers as (

    select * from {{ ref('stg_olist__sellers') }}

),

final as (

    select
        seller_id,
        seller_zip_code_prefix,
        seller_city,
        seller_state
    from sellers

)

select * from final
