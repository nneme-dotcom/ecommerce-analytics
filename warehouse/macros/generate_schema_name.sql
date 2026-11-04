{#-
    Schema naming (ADR-014, ADR-016, DATA_CONTRACT §3). dbt builds exactly three schemas: staging,
    core and marts. The default dbt behaviour (<target schema>_<custom schema>) is replaced:

    - models, seeds and snapshots must have a custom schema (set per folder in dbt_project.yml)
      and it must be one of the three. Anything else (no schema, a raw_* schema, a typo) is a
      compile error, so dbt can never write into the loaders' raw_* schemas.
    - `target_schema` / `target_database` (the legacy snapshot configs) are rejected on every
      node: dbt applies them *after* this macro, so they would bypass it.
    - other nodes (data tests, unit tests, analyses, functions) get the target schema, which must
      itself be one of the three (the profile fixes it to `staging`).

    dbt calls this macro while parsing, so every command (parse, compile, build, ...) fails before
    touching the database. assert_node_schemas (on-run-start) re-checks the final schemas.
-#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- set allowed = ["staging", "core", "marts"] -%}
    {%- for legacy in ["target_schema", "target_database"] -%}
        {%- if node.config.get(legacy) -%}
            {{ exceptions.raise_compiler_error(
                node.resource_type ~ " '" ~ node.unique_id ~ "' sets " ~ legacy
                ~ ", which bypasses generate_schema_name; use +schema (one of "
                ~ allowed | join(", ") ~ ") instead"
            ) }}
        {%- endif -%}
    {%- endfor -%}
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
        {%- set schema = target.schema | trim -%}
        {%- if schema not in allowed -%}
            {{ exceptions.raise_compiler_error(
                "target schema '" ~ schema ~ "' (profiles.yml) must be one of "
                ~ allowed | join(", ")
            ) }}
        {%- endif -%}
        {{ schema }}
    {%- endif -%}
{%- endmacro %}
