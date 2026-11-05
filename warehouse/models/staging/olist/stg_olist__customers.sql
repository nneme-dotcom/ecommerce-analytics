-- Grain: one row per customer_id (= one row per raw_olist.customers record).
-- customer_id is issued per order in Olist; customer_unique_id identifies the person.
-- Typed and cleaned only (DATA_CONTRACT §3): empty strings -> NULL, zip prefix left-padded to
-- 5 digits when shorter. No business logic.

with source as (

    select * from {{ source('olist', 'customers') }}

),

cleaned as (

    select
        nullif(customer_id, '') as customer_id,
        nullif(customer_unique_id, '') as customer_unique_id,
        nullif(customer_zip_code_prefix, '') as customer_zip_code_prefix,
        nullif(customer_city, '') as customer_city,
        nullif(customer_state, '') as customer_state
    from source

),

typed as (

    select
        customer_id,
        customer_unique_id,
        {{ zip_prefix('customer_zip_code_prefix') }} as customer_zip_code_prefix,
        customer_city,
        customer_state
    from cleaned

)

select * from typed
