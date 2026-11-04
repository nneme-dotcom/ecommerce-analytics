{#-
    Grain test: no two rows share the same values in `combination_of_columns` (a composite key).
    Use it at model level:
        data_tests:
          - unique_combination_of_columns:
              arguments:
                combination_of_columns: [rate_date, base_currency, quote_currency]
    Returns one row per duplicated key. A NULL in a key column is a separate not_null concern.
-#}
{% test unique_combination_of_columns(model, combination_of_columns) %}

{%- set columns_csv = combination_of_columns | join(", ") %}

with key_counts as (

    select
        {{ columns_csv }},
        count(*) as n_rows
    from {{ model }}
    group by {{ columns_csv }}

)

select *
from key_counts
where n_rows > 1

{% endtest %}
