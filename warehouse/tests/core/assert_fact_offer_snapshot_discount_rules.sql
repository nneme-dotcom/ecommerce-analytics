{#-
    Discount measures of core.fact_offer_snapshot (DATA_CONTRACT §3, ADR-019):
      - has_discount is true exactly when original_price > price (false, never NULL, when
        original_price is NULL or <= price);
      - then discount_amount = original_price - price (> 0) and discount_pct is in (0, 1)
        and equals the unrounded ratio (price > 0, so the discount is < 100 %; not rounded,
        so a tiny discount is never 0 while has_discount is true);
      - otherwise discount_amount and discount_pct are both NULL.
    Returns the offending rows with the reason.
-#}

with facts as (

    select * from {{ ref('fact_offer_snapshot') }}

),

checked as (

    select
        snapshot_date,
        product_id,
        item_id,
        price,
        original_price,
        has_discount,
        discount_amount,
        discount_pct,
        case
            when has_discount is null
                then 'has_discount is NULL'
            when has_discount <> coalesce(original_price > price, false)
                then 'has_discount does not follow original_price > price'
            when has_discount and (discount_amount is null or discount_pct is null)
                then 'discount without amount or pct'
            when has_discount and discount_amount <> original_price - price
                then 'discount_amount is not original_price - price'
            when has_discount and discount_amount <= 0
                then 'discount_amount not positive'
            when has_discount and not (discount_pct > 0 and discount_pct < 1)
                then 'discount_pct outside (0, 1)'
            when has_discount
                and discount_pct <> (original_price - price) / original_price
                then 'discount_pct is not the ratio (original_price - price) / original_price'
            when not has_discount and (discount_amount is not null or discount_pct is not null)
                then 'discount measure without a discount'
        end as problem
    from facts

)

select *
from checked
where problem is not null
