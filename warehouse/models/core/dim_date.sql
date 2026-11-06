-- Grain: one row per calendar day (date_day), every day from 1 January of the first year to
-- 31 December of the last year in which any of the five Olist order timestamps or any Mercado
-- Libre snapshot_date falls (conformed: shared by the Olist and ML facts; real data since task
-- 2.2: 2016-01-01 .. 2026-12-31, 4,018 rows; the years between the sources keep it contiguous).
-- Brazilian and Argentinian holiday flags from raw_enrichment.public_holidays, nationwide public
-- holidays only (ADR-018, ADR-019).

with orders as (

    select * from {{ ref('stg_olist__orders') }}

),

ml_runs as (

    select * from {{ ref('stg_mercadolibre__runs') }}

),

holidays as (

    select * from {{ ref('stg_enrichment__public_holidays') }}

),

-- least/greatest ignore NULLs, so one pass over the orders covers all five timestamps.
olist_bounds as (

    select
        min(least(
            order_purchase_timestamp,
            order_approved_at,
            order_delivered_carrier_date,
            order_delivered_customer_date,
            order_estimated_delivery_date
        ))::date as first_day,
        max(greatest(
            order_purchase_timestamp,
            order_approved_at,
            order_delivered_carrier_date,
            order_delivered_customer_date,
            order_estimated_delivery_date
        ))::date as last_day
    from orders

),

-- Every run status: a failed day is still a calendar day.
ml_bounds as (

    select
        min(snapshot_date) as first_day,
        max(snapshot_date) as last_day
    from ml_runs

),

-- Whole years around both sources (either may be empty: least/greatest ignore its NULLs).
bounds as (

    select
        date_trunc('year', least(olist_bounds.first_day, ml_bounds.first_day))::date
            as first_day,
        (
            date_trunc('year', greatest(olist_bounds.last_day, ml_bounds.last_day))
            + interval '1 year' - interval '1 day'
        )::date as last_day
    from olist_bounds
    cross join ml_bounds

),

days as (

    select day::date as date_day
    from bounds
    cross join generate_series(bounds.first_day, bounds.last_day, interval '1 day') as day

),

-- Nationwide public holidays per country: Nager.Date `global` = true and type `Public`.
-- Regional (counties) and optional/bank/observance entries are excluded. A date can carry
-- several entries, so names are aggregated to keep one row per country and date.
public_holidays as (

    select
        country_code,
        holiday_date,
        string_agg(distinct holiday_name, '; ' order by holiday_name) as holiday_name
    from holidays
    where country_code in ('BR', 'AR')
        and is_global
        and 'Public' = any(holiday_types)
    group by country_code, holiday_date

),

-- Years for which a country's calendar was loaded at all: outside them its flag is unknown
-- (NULL), not false.
calendar_years as (

    select distinct
        country_code,
        extract(year from holiday_date)::integer as calendar_year
    from holidays
    where country_code in ('BR', 'AR')

),

final as (

    select
        days.date_day,
        extract(isoyear from days.date_day)::integer as iso_year,
        extract(year from days.date_day)::integer as calendar_year,
        extract(quarter from days.date_day)::integer as calendar_quarter,
        extract(month from days.date_day)::integer as calendar_month,
        to_char(days.date_day, 'YYYY-MM') as year_month,
        to_char(days.date_day, 'FMMonth') as month_name,
        extract(week from days.date_day)::integer as iso_week,
        extract(isodow from days.date_day)::integer as iso_day_of_week,
        to_char(days.date_day, 'FMDay') as day_name,
        extract(isodow from days.date_day) in (6, 7) as is_weekend,
        case
            when br_years.calendar_year is null then null
            else br_holidays.holiday_date is not null
        end as is_br_public_holiday,
        br_holidays.holiday_name as br_holiday_name,
        case
            when ar_years.calendar_year is null then null
            else ar_holidays.holiday_date is not null
        end as is_ar_public_holiday,
        ar_holidays.holiday_name as ar_holiday_name
    from days
    left join public_holidays as br_holidays
        on br_holidays.country_code = 'BR'
        and br_holidays.holiday_date = days.date_day
    left join calendar_years as br_years
        on br_years.country_code = 'BR'
        and br_years.calendar_year = extract(year from days.date_day)::integer
    left join public_holidays as ar_holidays
        on ar_holidays.country_code = 'AR'
        and ar_holidays.holiday_date = days.date_day
    left join calendar_years as ar_years
        on ar_years.country_code = 'AR'
        and ar_years.calendar_year = extract(year from days.date_day)::integer

)

select * from final
