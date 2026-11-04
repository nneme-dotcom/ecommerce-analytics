{#-
    Contract conformance of every declared source table (DATA_CONTRACT §2, §2b).

    The columns declared in models/**/_*__sources.yml must be exactly the columns of the raw table
    in the database: same names, same order, and the same type when `data_type` is declared
    (compared with format_type(), e.g. `text`, `character(3)`, `text[]`). Raw tables are owned by
    the Python loaders, so a failure here means the source YAML, the loader DDL and the contract
    disagree: fix all three together.

    Returns one row per problem: a declared column missing from the table, an undeclared column
    in the table, a column at the wrong position, or a different type. A declared table that does
    not exist shows up as all of its columns missing.
-#}
{%- set expected_rows = [] -%}
{%- if execute -%}
    {%- for src in graph.sources.values() if src.package_name == project_name -%}
        {%- for column in src.columns.values() -%}
            {%- do expected_rows.append(
                "('" ~ src.schema ~ "', '" ~ src.identifier ~ "', '" ~ column.name ~ "', "
                ~ loop.index ~ ", "
                ~ ("'" ~ column.data_type ~ "'" if column.data_type else "null") ~ ")"
            ) -%}
        {%- endfor -%}
    {%- endfor -%}
{%- endif -%}

with expected (table_schema, table_name, column_name, position, data_type) as (

    {% if expected_rows %}
    values
        {{ expected_rows | join(",\n        ") }}
    {% else %}
    select null::text, null::text, null::text, null::bigint, null::text where false
    {% endif %}

),

declared_tables as (

    select distinct table_schema, table_name
    from expected

),

actual as (

    select
        n.nspname as table_schema,
        c.relname as table_name,
        a.attname as column_name,
        row_number() over (partition by n.nspname, c.relname order by a.attnum) as position,
        format_type(a.atttypid, a.atttypmod) as data_type
    from pg_catalog.pg_attribute as a
    inner join pg_catalog.pg_class as c on c.oid = a.attrelid
    inner join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
    inner join declared_tables as d
        on d.table_schema = n.nspname and d.table_name = c.relname
    where a.attnum > 0 and not a.attisdropped

),

compared as (

    select
        coalesce(e.table_schema, a.table_schema) as table_schema,
        coalesce(e.table_name, a.table_name) as table_name,
        coalesce(e.column_name, a.column_name) as column_name,
        e.position as expected_position,
        a.position as actual_position,
        e.data_type as expected_type,
        a.data_type as actual_type,
        case
            when a.column_name is null then 'declared but missing in the database'
            when e.column_name is null then 'in the database but not declared'
            when e.position <> a.position then 'wrong position'
            when e.data_type is not null and e.data_type <> a.data_type then 'wrong type'
        end as problem
    from expected as e
    full outer join actual as a
        on a.table_schema = e.table_schema
        and a.table_name = e.table_name
        and a.column_name = e.column_name

)

select *
from compared
where problem is not null
order by table_schema, table_name, coalesce(expected_position, actual_position)
