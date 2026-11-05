-- Grain: one row per order (order_id), every raw_olist.orders record whatever its status
-- (real data: 99,441 rows). Milestone timestamps and their dates (FK to dim_date), plus delivery
-- metrics for delivered orders only (ADR-018):
--   is_delivered    = order_status = 'delivered' and a customer delivery timestamp exists.
--   delivery_days   = delivered_customer_date - purchase_date (whole calendar days).
--   delay_days      = delivered_customer_date - estimated_delivery_date (negative = early).
--   is_on_time      = delivered_customer_date <= estimated_delivery_date.
-- Dates, not timestamps: Olist's estimate is a date (00:00:00), so a delivery during the
-- estimated day is on time. Metrics are NULL when is_delivered is false (canceled orders that
-- carry a delivery timestamp, delivered orders without one, orders still in transit).

with orders as (

    select * from {{ ref('stg_olist__orders') }}

),

customers as (

    select * from {{ ref('stg_olist__customers') }}

),

joined as (

    select
        orders.order_id,
        orders.customer_id,
        customers.customer_unique_id,
        customers.customer_zip_code_prefix,
        orders.order_status,
        orders.order_purchase_timestamp as purchased_at,
        orders.order_approved_at as approved_at,
        orders.order_delivered_carrier_date as delivered_carrier_at,
        orders.order_delivered_customer_date as delivered_customer_at,
        orders.order_estimated_delivery_date as estimated_delivery_at,
        orders.order_status = 'delivered'
        and orders.order_delivered_customer_date is not null as is_delivered
    from orders
    -- Left join: an order without its customer keeps its row and fails the not_null test.
    left join customers
        on customers.customer_id = orders.customer_id

),

dated as (

    select
        joined.*,
        purchased_at::date as purchase_date,
        approved_at::date as approved_date,
        delivered_carrier_at::date as delivered_carrier_date,
        delivered_customer_at::date as delivered_customer_date,
        estimated_delivery_at::date as estimated_delivery_date
    from joined

),

final as (

    select
        order_id,
        customer_id,
        customer_unique_id,
        customer_zip_code_prefix,
        order_status,
        purchased_at,
        approved_at,
        delivered_carrier_at,
        delivered_customer_at,
        estimated_delivery_at,
        purchase_date,
        approved_date,
        delivered_carrier_date,
        delivered_customer_date,
        estimated_delivery_date,
        is_delivered,
        case when is_delivered then delivered_customer_date - purchase_date end as delivery_days,
        case
            when is_delivered then delivered_customer_date - estimated_delivery_date
        end as delay_days,
        case
            when is_delivered then delivered_customer_date <= estimated_delivery_date
        end as is_on_time
    from dated

)

select * from final
