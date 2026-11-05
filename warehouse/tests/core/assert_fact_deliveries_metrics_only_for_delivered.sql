{#-
    core.fact_deliveries metric rules (DATA_CONTRACT §3, ADR-018):
      - is_delivered = (order_status = 'delivered' and delivered_customer_at is not null);
      - delivered orders have delivery_days, delay_days and is_on_time (the estimate is never
        NULL in Olist), with the date arithmetic of the contract;
      - every other order has all three NULL.
    Returns the offending orders with the reason.
-#}

with deliveries as (

    select * from {{ ref('fact_deliveries') }}

),

checked as (

    select
        order_id,
        order_status,
        is_delivered,
        delivery_days,
        delay_days,
        is_on_time,
        case
            when is_delivered
                <> (order_status = 'delivered' and delivered_customer_at is not null)
                then 'is_delivered does not match status and delivery timestamp'
            when not is_delivered
                and (delivery_days is not null or delay_days is not null or is_on_time is not null)
                then 'delivery metrics on an order that was not delivered'
            when is_delivered
                and (delivery_days is null or delay_days is null or is_on_time is null)
                then 'delivered order without delivery metrics'
            when is_delivered
                and (
                    delivery_days <> delivered_customer_date - purchase_date
                    or delay_days <> delivered_customer_date - estimated_delivery_date
                    or is_on_time <> (delay_days <= 0)
                )
                then 'delivery metrics do not follow the contract formulas'
        end as problem
    from deliveries

)

select *
from checked
where problem is not null
