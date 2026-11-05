{#-
    core.dim_date.is_br_public_holiday (DATA_CONTRACT §3, ADR-018):
      - NULL exactly in the years with no BR row in the holiday calendar (unknown, not false);
      - true exactly on the dates with a nationwide (is_global) BR entry of type Public, and then
        br_holiday_name is set; regional or non-Public entries never set it.
    Returns the offending days with the reason.
-#}

with dates as (

    select * from {{ ref('dim_date') }}

),

holidays as (

    select * from {{ ref('stg_enrichment__public_holidays') }}

),

br_years as (

    select distinct extract(year from holiday_date)::integer as calendar_year
    from holidays
    where country_code = 'BR'

),

nationwide as (

    select distinct holiday_date
    from holidays
    where country_code = 'BR' and is_global and 'Public' = any(holiday_types)

),

checked as (

    select
        dates.date_day,
        dates.is_br_public_holiday,
        dates.br_holiday_name,
        case
            when br_years.calendar_year is null and dates.is_br_public_holiday is not null
                then 'flag set in a year without a BR calendar'
            when br_years.calendar_year is not null and dates.is_br_public_holiday is null
                then 'flag NULL in a year with a BR calendar'
            when dates.is_br_public_holiday and nationwide.holiday_date is null
                then 'flagged but not a nationwide public holiday'
            when not dates.is_br_public_holiday and nationwide.holiday_date is not null
                then 'nationwide public holiday not flagged'
            when dates.is_br_public_holiday and dates.br_holiday_name is null
                then 'holiday without a name'
            when not dates.is_br_public_holiday and dates.br_holiday_name is not null
                then 'name on a non-holiday'
        end as problem
    from dates
    left join br_years
        on br_years.calendar_year = dates.calendar_year
    left join nationwide
        on nationwide.holiday_date = dates.date_day

)

select *
from checked
where problem is not null
