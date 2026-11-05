-- Grain: one row per calendar day (date_day), every day from 1 January of the first year to
-- 31 December of the last year in which any of the five Olist order timestamps falls
-- (real data: 2016-01-01 .. 2018-12-31, 1,096 rows). Brazilian holiday flag from
-- raw_enrichment.public_holidays, nationwide public holidays only (ADR-018).

with orders as (

    select * from {{ ref('stg_olist__orders') }}

),

holidays as (

    select * from {{ ref('stg_enrichment__public_holidays') }}

),

-- least/greatest ignore NULLs, so one pass over the orders covers all five timestamps.
bounds as (

    select
        date_trunc('year', min(least(
            order_purchase_timestamp,
            order_approved_at,
            order_delivered_carrier_date,
            order_delivered_customer_date,
            order_estimated_delivery_date
        )))::date as first_day,
        (date_trunc('year', max(greatest(
            order_purchase_timestamp,
            order_approved_at,
            order_delivered_carrier_date,
            order_delivered_customer_date,
            order_estimated_delivery_date
        ))) + interval '1 year' - interval '1 day')::date as last_day
    from orders

),

days as (

    select day::date as date_day
    from bounds
    cross join generate_series(bounds.first_day, bounds.last_day, interval '1 day') as day

),

-- Nationwide public holidays: Nager.Date `global` = true and type `Public`. Regional
-- (counties) and optional/bank/observance entries are excluded. A date can carry several
-- entries, so names are aggregated to keep one row per date.
br_public_holidays as (

    select
        holiday_date,
        string_agg(distinct holiday_name, '; ' order by holiday_name) as holiday_name
    from holidays
    where country_code = 'BR'
        and is_global
        and 'Public' = any(holiday_types)
    group by holiday_date

),

-- Years for which a BR calendar was loaded at all: outside them the flag is unknown (NULL),
-- not false.
br_calendar_years as (

    select distinct extract(year from holiday_date)::integer as calendar_year
    from holidays
    where country_code = 'BR'

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
            when br_calendar_years.calendar_year is null then null
            else br_public_holidays.holiday_date is not null
        end as is_br_public_holiday,
        br_public_holidays.holiday_name as br_holiday_name
    from days
    left join br_public_holidays
        on br_public_holidays.holiday_date = days.date_day
    left join br_calendar_years
        on br_calendar_years.calendar_year = extract(year from days.date_day)::integer

)

select * from final
