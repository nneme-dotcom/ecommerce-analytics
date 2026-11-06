{#-
    core.dim_date.is_br_public_holiday and is_ar_public_holiday (DATA_CONTRACT §3, ADR-018,
    ADR-019), for each country:
      - NULL exactly in the years with no row for that country in the holiday calendar
        (unknown, not false);
      - true exactly on the dates with a nationwide (is_global) entry of type Public, and then
        the holiday name is set; regional or non-Public entries never set it.
    Returns the offending (country, day) pairs with the reason.
-#}

with dates as (

    select * from {{ ref('dim_date') }}

),

holidays as (

    select * from {{ ref('stg_enrichment__public_holidays') }}

),

flags as (

    select 'BR' as country_code, date_day, calendar_year,
        is_br_public_holiday as is_holiday, br_holiday_name as holiday_name
    from dates
    union all
    select 'AR' as country_code, date_day, calendar_year,
        is_ar_public_holiday as is_holiday, ar_holiday_name as holiday_name
    from dates

),

calendar_years as (

    select distinct country_code, extract(year from holiday_date)::integer as calendar_year
    from holidays

),

nationwide as (

    select distinct country_code, holiday_date
    from holidays
    where is_global and 'Public' = any(holiday_types)

),

checked as (

    select
        flags.country_code,
        flags.date_day,
        flags.is_holiday,
        flags.holiday_name,
        case
            when calendar_years.calendar_year is null and flags.is_holiday is not null
                then 'flag set in a year without a calendar for the country'
            when calendar_years.calendar_year is not null and flags.is_holiday is null
                then 'flag NULL in a year with a calendar for the country'
            when flags.is_holiday and nationwide.holiday_date is null
                then 'flagged but not a nationwide public holiday'
            when not flags.is_holiday and nationwide.holiday_date is not null
                then 'nationwide public holiday not flagged'
            when flags.is_holiday and flags.holiday_name is null
                then 'holiday without a name'
            when not flags.is_holiday and flags.holiday_name is not null
                then 'name on a non-holiday'
        end as problem
    from flags
    left join calendar_years
        on calendar_years.country_code = flags.country_code
        and calendar_years.calendar_year = flags.calendar_year
    left join nationwide
        on nationwide.country_code = flags.country_code
        and nationwide.holiday_date = flags.date_day

)

select *
from checked
where problem is not null
