{#-
    Best-seller columns of core.fact_offer_snapshot re-derived from the highlights with a
    different method (distinct on, read from the source) (DATA_CONTRACT §3, ADR-019): for each
    fact row, is_best_seller says whether the product had a PRODUCT highlight that day;
    best_seller_category_id / best_seller_position are the entry with the lowest position (NULL
    positions last; ties: lowest category_id in byte order); best_seller_category_count is the
    number of such entries (0 if none). Also checks that offers_in_product is the number of
    the product's fact rows that day and that price_rank_in_product lies in
    1..offers_in_product. Returns the offending rows with the reason.
-#}

with facts as (

    select * from {{ ref('fact_offer_snapshot') }}

),

product_highlights as (

    select
        snapshot_date,
        nullif(product_id, '') as product_id,
        nullif(category_id, '') as category_id,
        position
    from {{ source('mercadolibre', 'highlights') }}
    where type = 'PRODUCT'

),

expected_best as (

    select distinct on (snapshot_date, product_id)
        snapshot_date,
        product_id,
        category_id,
        position
    from product_highlights
    order by snapshot_date, product_id, position asc nulls last, category_id collate "C"

),

expected_count as (

    select snapshot_date, product_id, count(*) as n_categories
    from product_highlights
    group by snapshot_date, product_id

),

product_rows as (

    select snapshot_date, product_id, count(*) as n_offers
    from facts
    group by snapshot_date, product_id

),

checked as (

    select
        facts.snapshot_date,
        facts.product_id,
        facts.item_id,
        case
            when facts.is_best_seller <> (expected_best.product_id is not null)
                then 'is_best_seller does not match the highlights'
            when facts.best_seller_category_id is distinct from expected_best.category_id
                then 'best_seller_category_id is not the best-ranked category'
            when facts.best_seller_position is distinct from expected_best.position
                then 'best_seller_position is not the best position'
            when facts.best_seller_category_count <> coalesce(expected_count.n_categories, 0)
                then 'best_seller_category_count is not the number of ranking categories'
            when facts.offers_in_product <> product_rows.n_offers
                then 'offers_in_product is not the number of offers of the product that day'
            when facts.price_rank_in_product not between 1 and facts.offers_in_product
                then 'price_rank_in_product outside 1..offers_in_product'
        end as problem
    from facts
    left join expected_best
        on expected_best.snapshot_date = facts.snapshot_date
        and expected_best.product_id = facts.product_id
    left join expected_count
        on expected_count.snapshot_date = facts.snapshot_date
        and expected_count.product_id = facts.product_id
    left join product_rows
        on product_rows.snapshot_date = facts.snapshot_date
        and product_rows.product_id = facts.product_id

)

select *
from checked
where problem is not null
