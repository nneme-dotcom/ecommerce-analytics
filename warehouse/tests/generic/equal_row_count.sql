{#-
    Reconciliation test (model level): the model has exactly as many rows as `compare_model`
    (a ref() or source()), or, with `compare_column`, as many rows as there are distinct non-null
    values of that column in `compare_model`. E.g. a fact built from a raw table must keep every
    raw record:
        data_tests:
          - equal_row_count:
              arguments:
                compare_model: source('olist', 'order_items')
    Returns one row with both counts when they differ.
-#}
{% test equal_row_count(model, compare_model, compare_column=none) %}

with model_rows as (

    select count(*) as n_rows from {{ model }}

),

compare_rows as (

    select
        {% if compare_column %}count(distinct {{ compare_column }}){% else %}count(*){% endif %}
            as n_rows
    from {{ compare_model }}

)

select
    model_rows.n_rows as model_rows,
    compare_rows.n_rows as compare_rows
from model_rows
cross join compare_rows
where model_rows.n_rows <> compare_rows.n_rows

{% endtest %}
