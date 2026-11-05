-- Grain: one row per order item (order_id, order_item_id) = one row per raw_olist.order_items
-- record. order_item_id is the 1-based position of the item inside its order; an order with two
-- units of the same product has two rows. Typed and cleaned only: empty strings -> NULL,
-- shipping_limit_date -> timestamp (local Brazilian time, no zone), price/freight -> numeric (BRL).

with source as (

    select * from {{ source('olist', 'order_items') }}

),

cleaned as (

    select
        nullif(order_id, '') as order_id,
        nullif(order_item_id, '') as order_item_id,
        nullif(product_id, '') as product_id,
        nullif(seller_id, '') as seller_id,
        nullif(shipping_limit_date, '') as shipping_limit_date,
        nullif(price, '') as price,
        nullif(freight_value, '') as freight_value
    from source

),

typed as (

    select
        order_id,
        order_item_id::integer as order_item_id,
        product_id,
        seller_id,
        shipping_limit_date::timestamp as shipping_limit_date,
        price::numeric as price,
        freight_value::numeric as freight_value
    from cleaned

)

select * from typed
