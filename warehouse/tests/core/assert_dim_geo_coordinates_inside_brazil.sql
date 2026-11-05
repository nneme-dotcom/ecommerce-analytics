{{ config(severity='warn') }}
{#-
    Plausibility (warning only): the per-prefix median point of core.dim_geo lies inside a
    bounding box around Brazil (lat -34..6, lng -74..-34). Raw geolocation has a few points far
    outside Brazil; the median should absorb them (ADR-018). A prefix listed here has mostly bad
    points: worth a look, not a build failure.
-#}

select zip_code_prefix, latitude, longitude
from {{ ref('dim_geo') }}
where latitude not between -34 and 6
    or longitude not between -74 and -34
