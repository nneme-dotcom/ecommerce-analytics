{#-
    core.dim_date has no gaps: one row per day from its first to its last day, starting on
    1 January and ending on 31 December (whole years, DATA_CONTRACT §3). Returns one row with the
    bounds and counts when that does not hold.
-#}

with bounds as (

    select
        min(date_day) as first_day,
        max(date_day) as last_day,
        count(*) as n_days
    from {{ ref('dim_date') }}

)

select *
from bounds
where n_days <> (last_day - first_day + 1)
    or extract(month from first_day) <> 1 or extract(day from first_day) <> 1
    or extract(month from last_day) <> 12 or extract(day from last_day) <> 31
