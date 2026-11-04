{#-
    Value test: every non-null value of the column is strictly greater than zero
    (e.g. FX rates, prices). Returns the offending rows.
-#}
{% test positive(model, column_name) %}

select *
from {{ model }}
where {{ column_name }} <= 0

{% endtest %}
