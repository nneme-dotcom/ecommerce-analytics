-- Grain: one row per snapshot day (snapshot_date) = one raw_mercadolibre.runs record, every
-- status (success, partial, failed). Typed in raw (§1b); payload and file_sha256 (loader
-- bookkeeping) are dropped. Which statuses count is a core decision (ADR-019).

with source as (

    select * from {{ source('mercadolibre', 'runs') }}

),

renamed as (

    select
        snapshot_date,
        run_id,
        nullif(status, '') as status,
        started_at,
        finished_at,
        nullif(site_id, '') as site_id,
        categories,
        categories_requested,
        requests_made,
        request_cap,
        request_cap_reached,
        row_counts,
        skipped_highlights,
        products_without_offers,
        errors,
        load_id
    from source

)

select * from renamed
