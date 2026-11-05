{#-
    True when (lat, lng) lies inside a bounding box around Brazil: latitude -34..6,
    longitude -74..-28 (bounds included). The eastern bound reaches past the mainland
    (about -34.8) to keep the oceanic islands, e.g. Fernando de Noronha (zip prefix 53990,
    longitude about -32.4). NULL when either coordinate is NULL.
    Used by core.dim_geo (coordinates outside the box are NULLed and flagged, ADR-018) and by
    its singular test, so both read the same box (DATA_CONTRACT §3).
-#}
{% macro inside_brazil_bbox(lat, lng) -%}
    ({{ lat }} between -34 and 6 and {{ lng }} between -74 and -28)
{%- endmacro %}
