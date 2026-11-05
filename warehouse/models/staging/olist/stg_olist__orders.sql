-- Grain: one row per order (order_id) = one row per raw_olist.orders record.
-- Typed and cleaned only: empty strings -> NULL, the five dates -> timestamp (local Brazilian time,
-- no zone; order_estimated_delivery_date carries a date only, at 00:00:00). Approval, carrier and
-- delivery timestamps are NULL for orders that never reached that step (and for a few that did).

with source as (

    select * from {{ source('olist', 'orders') }}

),

cleaned as (

    select
        nullif(order_id, '') as order_id,
        nullif(customer_id, '') as customer_id,
        nullif(order_status, '') as order_status,
        nullif(order_purchase_timestamp, '') as order_purchase_timestamp,
        nullif(order_approved_at, '') as order_approved_at,
        nullif(order_delivered_carrier_date, '') as order_delivered_carrier_date,
        nullif(order_delivered_customer_date, '') as order_delivered_customer_date,
        nullif(order_estimated_delivery_date, '') as order_estimated_delivery_date
    from source

),

typed as (

    select
        order_id,
        customer_id,
        order_status,
        order_purchase_timestamp::timestamp as order_purchase_timestamp,
        order_approved_at::timestamp as order_approved_at,
        order_delivered_carrier_date::timestamp as order_delivered_carrier_date,
        order_delivered_customer_date::timestamp as order_delivered_customer_date,
        order_estimated_delivery_date::timestamp as order_estimated_delivery_date
    from cleaned

)

select * from typed
