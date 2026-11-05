-- Grain: one row per product (product_id) = one row per raw_olist.products record.
-- Typed, cleaned and renamed only: the upstream typos product_name_lenght /
-- product_description_lenght become product_name_length / product_description_length; empty
-- strings -> NULL (category and text measures are NULL for some products); measures -> integer.
-- Measures are cast through numeric so that a value written as "40.0" still loads.

with source as (

    select * from {{ source('olist', 'products') }}

),

cleaned as (

    select
        nullif(product_id, '') as product_id,
        nullif(product_category_name, '') as product_category_name,
        nullif(product_name_lenght, '') as product_name_length,
        nullif(product_description_lenght, '') as product_description_length,
        nullif(product_photos_qty, '') as product_photos_qty,
        nullif(product_weight_g, '') as product_weight_g,
        nullif(product_length_cm, '') as product_length_cm,
        nullif(product_height_cm, '') as product_height_cm,
        nullif(product_width_cm, '') as product_width_cm
    from source

),

typed as (

    select
        product_id,
        product_category_name,
        product_name_length::numeric::integer as product_name_length,
        product_description_length::numeric::integer as product_description_length,
        product_photos_qty::numeric::integer as product_photos_qty,
        product_weight_g::numeric::integer as product_weight_g,
        product_length_cm::numeric::integer as product_length_cm,
        product_height_cm::numeric::integer as product_height_cm,
        product_width_cm::numeric::integer as product_width_cm
    from cleaned

)

select * from typed
