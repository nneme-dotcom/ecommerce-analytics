"""dbt guards of ADR-016, run with the real dbt on temporary copies of ``warehouse/``.

- ``generate_schema_name`` fails parsing (so every command) when a node would leave
  staging/core/marts, including the legacy snapshot ``target_schema`` / ``target_database``.
- ``assert_node_schemas`` (on-run-start) stops a run before any node executes even if the macro
  is weakened.
- ``assert_sources_match_contract`` reports a source column without ``data_type``.

Skipped when dbt is not installed (the python CI job); the dbt CI job runs them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("dbt.cli.main", reason="dbt not installed (requirements-dbt.txt)")

WAREHOUSE = Path(__file__).resolve().parents[2] / "warehouse"

SNAPSHOT = """
{{% snapshot snap_x %}}
{{{{ config({config}, unique_key='product_category_name', strategy='check', check_cols='all') }}}}
select * from {{{{ source('olist', 'product_category_name_translation') }}}}
{{% endsnapshot %}}
"""


@pytest.fixture
def project(tmp_path) -> Path:
    """A copy of the dbt project without build artifacts."""
    dest = tmp_path / "warehouse"
    shutil.copytree(
        WAREHOUSE,
        dest,
        ignore=shutil.ignore_patterns("target", "logs", "dbt_packages", "ci", "fixtures"),
    )
    return dest


def dbt(project: Path, *args: str, db_url: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DBT_", "PG"))}
    env["DBT_SEND_ANONYMOUS_USAGE_STATS"] = "false"
    if db_url is not None:
        from psycopg.conninfo import conninfo_to_dict

        info = conninfo_to_dict(db_url)
        env |= {
            "DBT_PG_HOST": str(info.get("host", "localhost")),
            "DBT_PG_PORT": str(info.get("port", "5432")),
            "DBT_PG_USER": str(info.get("user", "postgres")),
            "DBT_ENV_SECRET_PG_PASSWORD": str(info.get("password", "")),
            "DBT_PG_DBNAME": str(info["dbname"]),
            "DBT_PG_SSLMODE": "disable",
        }
    return subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [
            sys.executable,
            "-m",
            "dbt.cli.main",
            *args,
            "--project-dir",
            str(project),
            "--profiles-dir",
            str(project),
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- parse time (no database) -------------------------------------------------------------------


def test_the_project_parses(project):
    result = dbt(project, "parse")
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    ("config", "message"),
    [
        # The reviewer's case: a valid schema plus the legacy override.
        ("schema='staging', target_schema='raw_olist'", "sets target_schema"),
        ("target_schema='raw_olist'", "sets target_schema"),
        ("schema='staging', target_database='other_db'", "sets target_database"),
        ("schema='raw_olist'", "has schema 'raw_olist'"),
        ("tags=['no_schema']", "has schema ''"),
    ],
)
def test_snapshot_outside_the_dbt_schemas_fails_to_parse(project, config, message):
    write(project / "snapshots" / "snap_x.sql", SNAPSHOT.format(config=config))
    result = dbt(project, "parse")
    assert result.returncode != 0
    assert message in result.stdout


def test_target_schema_from_dbt_project_yml_fails_to_parse(project):
    write(project / "snapshots" / "snap_x.sql", SNAPSHOT.format(config="schema='staging'"))
    yml = project / "dbt_project.yml"
    yml.write_text(
        yml.read_text() + "\nsnapshots:\n  ecommerce_analytics:\n    +target_schema: raw_olist\n"
    )
    result = dbt(project, "parse")
    assert result.returncode != 0
    assert "sets target_schema" in result.stdout


@pytest.mark.parametrize(
    ("relative", "sql", "message"),
    [
        ("staging/leak.sql", "{{ config(schema='raw_olist') }} select 1 as x", "'raw_olist'"),
        ("other/leak.sql", "select 1 as x", "has schema ''"),
    ],
)
def test_model_outside_the_dbt_schemas_fails_to_parse(project, relative, sql, message):
    write(project / "models" / relative, sql)
    result = dbt(project, "parse")
    assert result.returncode != 0
    assert message in result.stdout


def test_target_schema_outside_the_dbt_schemas_fails_to_parse(project):
    profiles = project / "profiles.yml"
    profiles.write_text(profiles.read_text().replace("schema: staging", "schema: raw_olist"))
    result = dbt(project, "parse")
    assert result.returncode != 0
    assert "target schema 'raw_olist'" in result.stdout


# --- run time (database) ------------------------------------------------------------------------


def test_on_run_start_stops_a_run_if_the_macro_is_weakened(project, clean_db, database_url):
    # Simulate a regression: the dbt default-like macro that accepts any custom schema.
    write(
        project / "macros" / "generate_schema_name.sql",
        "{% macro generate_schema_name(custom_schema_name, node) -%}"
        "{{ (custom_schema_name or target.schema) | trim }}{%- endmacro %}",
    )
    write(project / "models" / "staging" / "leak.sql", "{{ config(schema='raw_olist') }} select 1")
    result = dbt(project, "run", "--select", "leak", db_url=database_url)
    assert result.returncode != 0
    assert "model.ecommerce_analytics.leak -> schema 'raw_olist'" in result.stdout
    assert clean_db.execute("SELECT to_regclass('raw_olist.leak')").fetchone() == (None,)


def test_contract_test_reports_a_column_without_data_type(project, clean_db, database_url):
    from warehouse.ci import fixture_db

    fixture_db.load_fixtures(clean_db, fixture_db.prepare_fixtures())
    sources = project / "models" / "staging" / "olist" / "_olist__sources.yml"
    text = sources.read_text()
    assert text.count("{name: seller_city, data_type: text}") == 1
    sources.write_text(text.replace("{name: seller_city, data_type: text}", "{name: seller_city}"))

    select = ("--select", "assert_sources_match_contract")
    result = dbt(project, "compile", *select, db_url=database_url)
    assert result.returncode == 0, result.stdout
    compiled = next((project / "target" / "compiled").rglob("assert_sources_match_contract.sql"))
    rows = clean_db.execute(
        f"SELECT table_name, column_name, problem FROM ({compiled.read_text()}) AS t"  # noqa: S608
    ).fetchall()
    assert rows == [("sellers", "seller_city", "no data_type declared")]

    result = dbt(project, "test", *select, db_url=database_url)
    assert result.returncode != 0
