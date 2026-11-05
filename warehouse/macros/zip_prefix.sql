{#-
    Normalise an Olist zip code prefix (text) to its 5-digit form: a value shorter than 5
    characters is left-padded with zeros ('1310' -> '01310'), anything else is returned unchanged
    (lpad would silently truncate a longer value). NULL stays NULL. The staging models test the
    result against ^[0-9]{5}$, so a malformed value fails the build instead of being "fixed".
    The real CSVs already carry 5 digits; this guards the joins between geolocation, customers and
    sellers against a source that drops leading zeros (DATA_CONTRACT §3).
-#}
{% macro zip_prefix(column) -%}
    case
        when length({{ column }}) < 5 then lpad({{ column }}, 5, '0')
        else {{ column }}
    end
{%- endmacro %}
