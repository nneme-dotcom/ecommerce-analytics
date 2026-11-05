{#-
    Value test: every non-null value of the column is zero or more (e.g. freight, payment value,
    delivery days). Returns the offending rows.
-#}
{% test not_negative(model, column_name) %}

select *
from {{ model }}
where {{ column_name }} < 0

{% endtest %}
