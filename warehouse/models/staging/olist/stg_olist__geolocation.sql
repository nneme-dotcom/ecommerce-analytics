-- Grain: one row per raw_olist.geolocation record. NOT unique: a zip prefix has many rows
-- (different points, accent variants of the city, exact duplicates). core.dim_geo aggregates
-- them to one row per prefix (ADR-018). Typed and cleaned only: empty strings -> NULL,
-- lat/lng -> double precision, zip prefix padded to 5 digits when shorter.

with source as (

    select * from {{ source('olist', 'geolocation') }}

),

cleaned as (

    select
        nullif(geolocation_zip_code_prefix, '') as geolocation_zip_code_prefix,
        nullif(geolocation_lat, '') as geolocation_lat,
        nullif(geolocation_lng, '') as geolocation_lng,
        nullif(geolocation_city, '') as geolocation_city,
        nullif(geolocation_state, '') as geolocation_state
    from source

),

typed as (

    select
        {{ zip_prefix('geolocation_zip_code_prefix') }} as geolocation_zip_code_prefix,
        geolocation_lat::double precision as geolocation_lat,
        geolocation_lng::double precision as geolocation_lng,
        geolocation_city,
        geolocation_state
    from cleaned

)

select * from typed
