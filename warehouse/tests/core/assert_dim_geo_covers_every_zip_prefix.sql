{#-
    core.dim_geo holds exactly the zip prefixes found in geolocation, customers and sellers
    (row-count reconciliation for an aggregated dimension, DATA_CONTRACT §3): none missing,
    none invented, and has_geolocation is true exactly for the prefixes present in geolocation.
    Returns the offending prefixes with the reason.
-#}

with geolocation_prefixes as (

    select distinct geolocation_zip_code_prefix as zip_code_prefix
    from {{ ref('stg_olist__geolocation') }}

),

source_prefixes as (

    select zip_code_prefix from geolocation_prefixes
    union
    select customer_zip_code_prefix from {{ ref('stg_olist__customers') }}
    union
    select seller_zip_code_prefix from {{ ref('stg_olist__sellers') }}

),

dim as (

    select zip_code_prefix, has_geolocation from {{ ref('dim_geo') }}

),

compared as (

    select
        coalesce(source_prefixes.zip_code_prefix, dim.zip_code_prefix) as zip_code_prefix,
        case
            when dim.zip_code_prefix is null then 'missing from dim_geo'
            when source_prefixes.zip_code_prefix is null then 'in dim_geo but in no source'
            when dim.has_geolocation <> (geolocation_prefixes.zip_code_prefix is not null)
                then 'has_geolocation is wrong'
        end as problem
    from source_prefixes
    full outer join dim
        on dim.zip_code_prefix = source_prefixes.zip_code_prefix
    left join geolocation_prefixes
        on geolocation_prefixes.zip_code_prefix = dim.zip_code_prefix

)

select *
from compared
where problem is not null
