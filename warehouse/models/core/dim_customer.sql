-- Grain: one row per person (customer_unique_id). Real data: 96,096 rows from 99,441 customer_ids.
-- In Olist a customer_id is issued per order (customers : orders = 1 : 1), so the same person
-- placing a second order gets a new customer_id with the same customer_unique_id. Repeat-customer
-- and cohort analysis must therefore count customer_unique_id, never customer_id. The facts keep
-- customer_id as a degenerate attribute and join here on customer_unique_id (ADR-018).
-- Location = the customer_id row of the person's latest purchase (ties and customer_ids without
-- an order: lowest customer_id, byte order). The location at the time of each order is on the
-- facts (customer_zip_code_prefix).

with customers as (

    select * from {{ ref('stg_olist__customers') }}

),

orders as (

    select * from {{ ref('stg_olist__orders') }}

),

customer_orders as (

    select
        customers.customer_unique_id,
        customers.customer_id,
        customers.customer_zip_code_prefix,
        customers.customer_city,
        customers.customer_state,
        orders.order_purchase_timestamp
    from customers
    left join orders
        on orders.customer_id = customers.customer_id

),

ranked as (

    select
        customer_orders.*,
        row_number() over (
            partition by customer_unique_id
            order by order_purchase_timestamp desc nulls last, customer_id collate "C"
        ) as recency_rank,
        count(*) over (partition by customer_unique_id) as customer_id_count,
        min(order_purchase_timestamp) over (partition by customer_unique_id) as first_purchase_at,
        max(order_purchase_timestamp) over (partition by customer_unique_id) as last_purchase_at
    from customer_orders

),

final as (

    select
        customer_unique_id,
        customer_zip_code_prefix,
        customer_city,
        customer_state,
        customer_id_count,
        first_purchase_at,
        last_purchase_at
    from ranked
    where recency_rank = 1

)

select * from final
