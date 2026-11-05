{#-
    Format test: every non-null value of the column matches the POSIX regular expression
    `pattern` (PostgreSQL `~`), e.g. pattern: '^[0-9]{5}$' for a zip code prefix.
    Returns the offending rows.
-#}
{% test matches_regex(model, column_name, pattern) %}

select *
from {{ model }}
where {{ column_name }} is not null
    and not ({{ column_name }}::text ~ '{{ pattern }}')

{% endtest %}
