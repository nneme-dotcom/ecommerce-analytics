-- Grain: one row per Portuguese product category name (product_category_name).
-- Source: raw_olist.product_category_name_translation (DATA_CONTRACT §2); the loader already
-- stripped the UTF-8 BOM from the first column name. Cleaned only: empty strings -> NULL.
-- Two categories used in raw_olist.products have no row here (see core.dim_product).

with source as (

    select * from {{ source('olist', 'product_category_name_translation') }}

),

cleaned as (

    select
        nullif(product_category_name, '') as product_category_name,
        nullif(product_category_name_english, '') as product_category_name_english
    from source

)

select * from cleaned
