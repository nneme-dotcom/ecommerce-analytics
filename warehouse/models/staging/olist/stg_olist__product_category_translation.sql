-- Grain: one row per Portuguese product category name (product_category_name).
-- Source: raw_olist.product_category_name_translation (DATA_CONTRACT §2), unchanged apart from
-- explicit column selection. Task 2.0 smoke model; task 2.1 builds the rest of staging.

with source as (

    select * from {{ source('olist', 'product_category_name_translation') }}

),

renamed as (

    select
        product_category_name,
        product_category_name_english
    from source

)

select * from renamed
