{#-
    Second check of ADR-016's schema rule, run as on-run-start (dbt_project.yml) by build, run,
    seed, snapshot and test before any node executes. generate_schema_name is the primary guard
    (it fails at parse time); this one reads the schema and database each node finally resolved
    to, after every dbt override, so a weakened or bypassed macro still stops the run.

    Fails if any model, seed, snapshot, test or function in the project resolves to a schema other
    than staging/core/marts or to a database other than the target's. Returns no SQL.
-#}
{% macro assert_node_schemas() -%}
    {%- if execute -%}
        {%- set allowed = ["staging", "core", "marts"] -%}
        {%- set problems = [] -%}
        {%- for node in graph.nodes.values() if node.package_name == project_name -%}
            {%- if node.resource_type in ["model", "seed", "snapshot", "test", "function"] -%}
                {%- if node.schema not in allowed -%}
                    {%- do problems.append(node.unique_id ~ " -> schema '" ~ node.schema ~ "'") -%}
                {%- endif -%}
                {%- if node.database and node.database != target.database -%}
                    {%- do problems.append(
                        node.unique_id ~ " -> database '" ~ node.database ~ "'"
                    ) -%}
                {%- endif -%}
            {%- endif -%}
        {%- endfor -%}
        {%- if problems -%}
            {{ exceptions.raise_compiler_error(
                "nodes outside " ~ allowed | join("/") ~ " (ADR-016): " ~ problems | join("; ")
            ) }}
        {%- endif -%}
    {%- endif -%}
{%- endmacro %}
