-- Grain: one row per seller (seller_id) = one row per raw_olist.sellers record.
-- Typed and cleaned only: empty strings -> NULL, zip prefix padded to 5 digits when shorter.

with source as (

    select * from {{ source('olist', 'sellers') }}

),

cleaned as (

    select
        nullif(seller_id, '') as seller_id,
        nullif(seller_zip_code_prefix, '') as seller_zip_code_prefix,
        nullif(seller_city, '') as seller_city,
        nullif(seller_state, '') as seller_state
    from source

),

typed as (

    select
        seller_id,
        {{ zip_prefix('seller_zip_code_prefix') }} as seller_zip_code_prefix,
        seller_city,
        seller_state
    from cleaned

)

select * from typed
