-- Grain: one row per payment of an order (order_id, payment_sequential) = one row per
-- raw_olist.order_payments record. An order paid with a voucher plus a card has two rows.
-- Typed and cleaned only: empty strings -> NULL, counters -> integer, payment_value -> numeric (BRL).

with source as (

    select * from {{ source('olist', 'order_payments') }}

),

cleaned as (

    select
        nullif(order_id, '') as order_id,
        nullif(payment_sequential, '') as payment_sequential,
        nullif(payment_type, '') as payment_type,
        nullif(payment_installments, '') as payment_installments,
        nullif(payment_value, '') as payment_value
    from source

),

typed as (

    select
        order_id,
        payment_sequential::integer as payment_sequential,
        payment_type,
        payment_installments::integer as payment_installments,
        payment_value::numeric as payment_value
    from cleaned

)

select * from typed
