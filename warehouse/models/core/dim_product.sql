-- Grain: one row per product (product_id). Real data: 32,951 rows. Natural key from Olist.
-- English category from the translation table. product_category_name_english is NULL when the
-- product has no category (610 real products) or its category has no translation (2 real
-- categories: pc_gamer, portateis_cozinha_e_preparadores_de_alimentos); has_category_translation
-- tells the two apart from a translated category. No label is invented here (marts decide).

with products as (

    select * from {{ ref('stg_olist__products') }}

),

translations as (

    select * from {{ ref('stg_olist__product_category_translation') }}

),

final as (

    select
        products.product_id,
        products.product_category_name,
        translations.product_category_name_english,
        translations.product_category_name is not null as has_category_translation,
        products.product_name_length,
        products.product_description_length,
        products.product_photos_qty,
        products.product_weight_g,
        products.product_length_cm,
        products.product_height_cm,
        products.product_width_cm
    from products
    left join translations
        on translations.product_category_name = products.product_category_name

)

select * from final
