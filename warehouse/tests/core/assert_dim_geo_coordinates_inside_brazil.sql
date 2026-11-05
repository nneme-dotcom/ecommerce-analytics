{#-
    core.dim_geo coordinate rule (DATA_CONTRACT §3, ADR-018): every coordinate it publishes lies
    inside Brazil's bounding box (macro inside_brazil_bbox), and has_invalid_coordinates names
    the prefixes whose median point did not, which therefore have NULL coordinates:
      - latitude and longitude are NULL or set together;
      - set coordinates lie inside the box;
      - a flagged prefix has geolocation rows and NULL coordinates;
      - an unflagged prefix has coordinates exactly when it has geolocation rows.
    Whether a median lies outside the box is decided in the model (and unit-tested); this test
    checks the published result. Until review round 1 of 2.1 it was a warning that fired on
    8 real prefixes forever; now those are flagged and it is an error. Returns the offending
    prefixes with the reason.
-#}

with geo as (

    select * from {{ ref('dim_geo') }}

),

checked as (

    select
        zip_code_prefix,
        latitude,
        longitude,
        has_geolocation,
        has_invalid_coordinates,
        case
            when (latitude is null) <> (longitude is null)
                then 'only one of latitude / longitude is set'
            when latitude is not null
                and not {{ inside_brazil_bbox('latitude', 'longitude') }}
                then 'coordinates outside the Brazil bounding box'
            when has_invalid_coordinates and not has_geolocation
                then 'flagged without geolocation rows'
            when has_invalid_coordinates and latitude is not null
                then 'flagged but coordinates kept'
            when not has_invalid_coordinates and has_geolocation and latitude is null
                then 'geolocation rows but no coordinates and no flag'
            when not has_geolocation and latitude is not null
                then 'coordinates without geolocation rows'
        end as problem
    from geo

)

select *
from checked
where problem is not null
