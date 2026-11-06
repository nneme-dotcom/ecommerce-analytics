{#-
    The Mercado Libre dimensions hold exactly the ids of their sources on the included days
    (run status success or partial; DATA_CONTRACT §3, ADR-019), read from raw:
      - dim_ml_product: product ids of products, offers and PRODUCT highlights;
      - dim_ml_seller: non-NULL seller ids of offers;
      - dim_ml_category: category ids of highlights (any type) and offers.
    A missing id would break a fact relationship; an extra one would be a member nobody saw.
    Returns one row per (dimension, id) that is missing or extra.
-#}

with included_days as (

    select snapshot_date
    from {{ source('mercadolibre', 'runs') }}
    where status in {{ ml_included_run_statuses() }}

),

expected as (

    select 'dim_ml_product' as dimension, nullif(product_id, '') as id, snapshot_date
    from {{ source('mercadolibre', 'products') }}
    union
    select 'dim_ml_product', nullif(product_id, ''), snapshot_date
    from {{ source('mercadolibre', 'offers') }}
    union
    select 'dim_ml_product', nullif(product_id, ''), snapshot_date
    from {{ source('mercadolibre', 'highlights') }}
    where type = 'PRODUCT'
    union
    select 'dim_ml_seller', seller_id::text, snapshot_date
    from {{ source('mercadolibre', 'offers') }}
    where seller_id is not null
    union
    select 'dim_ml_category', nullif(category_id, ''), snapshot_date
    from {{ source('mercadolibre', 'highlights') }}
    union
    select 'dim_ml_category', nullif(category_id, ''), snapshot_date
    from {{ source('mercadolibre', 'offers') }}
    where nullif(category_id, '') is not null

),

expected_ids as (

    select distinct expected.dimension, expected.id
    from expected
    inner join included_days
        on included_days.snapshot_date = expected.snapshot_date

),

actual_ids as (

    select 'dim_ml_product' as dimension, product_id as id from {{ ref('dim_ml_product') }}
    union all
    select 'dim_ml_seller', seller_id::text from {{ ref('dim_ml_seller') }}
    union all
    select 'dim_ml_category', category_id from {{ ref('dim_ml_category') }}

)

select
    coalesce(expected_ids.dimension, actual_ids.dimension) as dimension,
    coalesce(expected_ids.id, actual_ids.id) as id,
    case when actual_ids.id is null then 'missing' else 'extra' end as problem
from expected_ids
full outer join actual_ids
    on actual_ids.dimension = expected_ids.dimension
    and actual_ids.id = expected_ids.id
where expected_ids.id is null or actual_ids.id is null
