{#-
    Row rule test (model level): the boolean SQL `expression` is true on every row, e.g.
        data_tests:
          - expression_is_true:
              arguments:
                expression: is_ranking_category or is_listing_category
    A row where it is false or NULL fails. Returns the offending rows.
-#}
{% test expression_is_true(model, expression) %}

select *
from {{ model }}
where not coalesce(({{ expression }}), false)

{% endtest %}
