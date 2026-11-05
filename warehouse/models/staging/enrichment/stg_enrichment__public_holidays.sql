-- Grain: one row per raw_enrichment.public_holidays entry (no key: the same date can appear for
-- several counties or types). Already typed in raw (§2b); all types and regional entries are
-- kept. Filtering to nationwide public holidays happens in core.dim_date.

with source as (

    select * from {{ source('enrichment', 'public_holidays') }}

),

renamed as (

    select
        country_code::text as country_code,
        holiday_date,
        local_name,
        name as holiday_name,
        is_global,
        counties,
        holiday_types,
        is_fixed,
        launch_year
    from source

)

select * from renamed
