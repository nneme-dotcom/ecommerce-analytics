-- Grain: one row per catalog product fetched per day (snapshot_date, product_id) = one
-- raw_mercadolibre.products record (the snapshot fetches each product once per run).

with source as (

    select * from {{ source('mercadolibre', 'products') }}

),

renamed as (

    select
        snapshot_date,
        run_id,
        line_no,
        captured_at,
        nullif(product_id, '') as product_id,
        nullif(name, '') as name,
        nullif(domain_id, '') as domain_id,
        nullif(family_name, '') as family_name,
        nullif(status, '') as status,
        nullif(brand, '') as brand,
        nullif(model, '') as model,
        nullif(permalink, '') as permalink
    from source

)

select * from renamed
