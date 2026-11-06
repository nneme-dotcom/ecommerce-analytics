{#-
    Mercado Libre run statuses whose days count in core (DATA_CONTRACT §3, ADR-019): `success`
    and `partial` (ADR-007). `failed` days captured no category and are left out. Renders a SQL
    list for `status in {{ ml_included_run_statuses() }}`; used by every ML core model and test,
    so the rule lives in one place.
-#}
{% macro ml_included_run_statuses() -%}
('success', 'partial')
{%- endmacro %}
