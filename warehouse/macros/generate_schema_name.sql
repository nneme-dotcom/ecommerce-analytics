{#-
    Schema naming (ADR-014, DATA_CONTRACT §3). dbt builds exactly three schemas: staging, core and
    marts. The default dbt behaviour (<target schema>_<custom schema>) is replaced:

    - models, seeds and snapshots must have a custom schema (set per folder in dbt_project.yml)
      and it must be one of the three. Anything else (no schema, a raw_* schema, a typo) is a
      compile error, so dbt can never write into the loaders' raw_* schemas.
    - other nodes (data tests, unit tests, analyses) are never materialised unless store_failures
      is on; they get the target schema, which the profile fixes to `staging`.
-#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- set allowed = ["staging", "core", "marts"] -%}
    {%- if node.resource_type in ["model", "seed", "snapshot"] -%}
        {%- set schema = (custom_schema_name or "") | trim -%}
        {%- if schema not in allowed -%}
            {{ exceptions.raise_compiler_error(
                node.resource_type ~ " '" ~ node.unique_id ~ "' has schema '" ~ schema
                ~ "'; it must be one of " ~ allowed | join(", ")
                ~ " (set +schema for its folder in dbt_project.yml)"
            ) }}
        {%- endif -%}
        {{ schema }}
    {%- else -%}
        {{ target.schema | trim }}
    {%- endif -%}
{%- endmacro %}
