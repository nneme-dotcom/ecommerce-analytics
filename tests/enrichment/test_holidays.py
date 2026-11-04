"""Holidays: mocked Nager.Date v3 (shape from the official README, ADR-013) + throwaway DB."""

from datetime import date

import httpx
import pytest

from ingestion.enrichment import holidays
from ingestion.enrichment.holidays import (
    default_plan,
    fetch_holidays,
    load_holidays,
    parse_holidays,
)
from ingestion.enrichment.http import FetchError, JsonClient


def v3_payload(country: str, year: int) -> list[dict]:
    """Entries in the documented v3 shape (official Nager.Date README)."""
    return [
        {
            "date": f"{year}-01-01",
            "localName": "Año Nuevo",
            "name": "New Year's Day",
            "countryCode": country,
            "fixed": True,
            "global": True,
            "counties": None,
            "launchYear": None,
            "types": ["Public"],
        },
        {
            "date": f"{year}-07-09",
            "localName": "Feriado regional",
            "name": "Regional Day",
            "countryCode": country,
            "fixed": False,
            "global": False,
            "counties": [f"{country}-SP", f"{country}-RJ"],
            "launchYear": 1997,
            "types": ["Public", "Bank"],
        },
    ]


class FakeNager:
    def __init__(self, empty: set[tuple[str, int]] = frozenset()):
        self.paths: list[str] = []
        self.empty = empty

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        _, _, _, _, year, country = request.url.path.split("/")
        if (country, int(year)) in self.empty:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=v3_payload(country, int(year)))


def client_for(handler) -> JsonClient:
    return JsonClient(
        "https://nager.test/api/v3",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda s: None,
    )


def test_default_plan():
    assert default_plan(date(2026, 11, 3)) == [
        ("BR", 2016),
        ("BR", 2017),
        ("BR", 2018),
        ("AR", 2026),
        ("AR", 2027),
    ]


def test_fetch_uses_v3_year_before_country():
    fake = FakeNager()
    rows = fetch_holidays(client_for(fake), [("BR", 2017), ("AR", 2026)])
    assert fake.paths == ["/api/v3/PublicHolidays/2017/BR", "/api/v3/PublicHolidays/2026/AR"]
    assert len(rows) == 4
    new_year = next(r for r in rows if r.country_code == "AR" and r.holiday_date.month == 1)
    assert new_year.counties is None  # null stays null
    assert new_year.is_global is True
    assert new_year.local_name == "Año Nuevo"
    assert new_year.holiday_types == ("Public",)
    regional = next(r for r in rows if r.country_code == "BR" and r.holiday_date.month == 7)
    assert regional.counties == ("BR-SP", "BR-RJ")
    assert (regional.is_fixed, regional.launch_year) == (False, 1997)


def drop(entry: dict, field: str) -> dict:
    return {k: v for k, v in entry.items() if k != field}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: {"not": "a list"}, "expected a JSON array"),
        (lambda p: [{**p[0], "date": "2018-01-01"}], "is not in 2017"),
        (lambda p: [{**p[0], "date": None}], "invalid date"),
        (lambda p: [{**p[0], "date": "2017-01-01T00:00:00"}], "invalid date"),
        (lambda p: [{**p[0], "name": ""}], "missing name"),
        (lambda p: [drop(p[0], "countryCode")], "countryCode None"),
        (lambda p: [{**p[0], "countryCode": "AR"}], "countryCode 'AR'"),
        (lambda p: [drop(p[0], "global")], "global is missing or not a boolean"),
        (lambda p: [{**p[0], "global": "yes"}], "global is missing or not a boolean"),
        (lambda p: [drop(p[0], "types")], "types is not a list of strings"),
        (lambda p: [{**p[0], "types": []}], "types is empty"),
        (lambda p: [{**p[0], "types": "Public"}], "types is not a list of strings"),
        (lambda p: [{**p[0], "counties": "BR-SP"}], "counties is not a list of strings"),
        (lambda p: [{**p[0], "localName": 5}], "localName is not str"),
        (lambda p: [{**p[0], "launchYear": True}], "launchYear is not int"),
        (lambda p: [{**p[0], "fixed": "no"}], "fixed is not bool"),
        (lambda p: [], "no holidays returned"),
        # A v4-shaped answer must not load silently.
        (
            lambda p: [
                {
                    "date": "2017-01-01",
                    "name": "New Year's Day",
                    "countryCode": "BR",
                    "nationalHoliday": True,
                    "subdivisionCodes": None,
                    "holidayTypes": ["Public"],
                }
            ],
            "global is missing",
        ),
    ],
)
def test_parse_rejects_bad_payloads(mutate, message):
    with pytest.raises(FetchError, match=message):
        parse_holidays(mutate(v3_payload("BR", 2017)), "BR", 2017)


def test_optional_fields_may_be_absent():
    [h] = parse_holidays(
        [
            {
                "date": "2017-04-21",
                "name": "Tiradentes",
                "countryCode": "BR",
                "global": True,
                "types": ["Public"],
            }
        ],
        "BR",
        2017,
    )
    assert (h.local_name, h.counties, h.is_fixed, h.launch_year) == (None, None, None, None)


def test_only_option_parsing():
    args = holidays.build_parser().parse_args(["--only", "br:2016-2018", "--only", "AR:2026"])
    assert args.only == [[("BR", 2016), ("BR", 2017), ("BR", 2018)], [("AR", 2026)]]
    with pytest.raises(SystemExit):
        holidays.build_parser().parse_args(["--only", "BRA:2016"])


def test_main_csv_only(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    out = tmp_path / "h.csv"
    assert (
        holidays.main(["--only", "BR:2017", "--csv-out", str(out)], client=client_for(FakeNager()))
        == 0
    )
    lines = out.read_text().splitlines()
    assert lines[0] == (
        "country_code,holiday_date,local_name,name,is_global,counties,holiday_types,"
        "is_fixed,launch_year,source"
    )
    assert lines[1] == "BR,2017-01-01,Año Nuevo,New Year's Day,true,,Public,true,,Nager.Date v3"
    assert lines[2] == (
        "BR,2017-07-09,Feriado regional,Regional Day,false,BR-SP;BR-RJ,Public;Bank,"
        "false,1997,Nager.Date v3"
    )


def test_main_empty_answer_fails_without_loading(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    fake = FakeNager(empty={("BR", 2018)})
    code = holidays.main(
        ["--only", "BR:2016-2018", "--csv-out", str(tmp_path / "h.csv")], client=client_for(fake)
    )
    assert code == 1
    assert not (tmp_path / "h.csv").exists()


# --- database ---------------------------------------------------------------------------------


def holiday_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT country_code, holiday_date, name, is_global, counties, holiday_types,"
            " local_name, is_fixed, launch_year FROM raw_enrichment.public_holidays"
            " ORDER BY 1, 2, 3"
        )
        return cur.fetchall()


def test_load_is_idempotent_and_scoped_to_requested_country_years(db_conn):
    plan = [("BR", 2016), ("BR", 2017), ("AR", 2026)]
    rows = fetch_holidays(client_for(FakeNager()), plan)
    load_holidays(db_conn, rows, plan)
    first = holiday_table(db_conn)
    assert len(first) == 6
    assert first[0][3:] == (True, None, ["Public"], "Año Nuevo", True, None)
    assert first[1][3:] == (
        False,
        ["AR-SP", "AR-RJ"],
        ["Public", "Bank"],
        "Feriado regional",
        False,
        1997,
    )
    load_holidays(db_conn, rows, plan)
    assert holiday_table(db_conn) == first

    # Reloading BR 2017 alone with a different answer leaves BR 2016 and AR 2026 untouched.
    single = [("BR", 2017)]
    load_holidays(db_conn, parse_holidays(v3_payload("BR", 2017)[:1], "BR", 2017), single)
    after = holiday_table(db_conn)
    assert len(after) == 5
    assert [r for r in after if r[1].year != 2017] == [r for r in first if r[1].year != 2017]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT row_count, params->'countries_years' FROM raw_enrichment._load_audit "
            "WHERE table_name = 'public_holidays' ORDER BY loaded_at DESC LIMIT 1"
        )
        assert cur.fetchone() == (1, ["BR:2017"])


def test_main_loads_into_database(database_url, db_conn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    assert holidays.main(["--only", "BR:2016-2018"], client=client_for(FakeNager())) == 0
    assert len(holiday_table(db_conn)) == 6


def test_drifted_table_is_recreated(db_conn):
    with db_conn.cursor() as cur:
        cur.execute("CREATE SCHEMA raw_enrichment")
        cur.execute(
            "CREATE TABLE raw_enrichment.public_holidays (country_code char(2), holiday_date date,"
            " name text, national_holiday boolean, subdivision_codes text[], holiday_types text[],"
            " source text)"
        )
    plan = [("BR", 2017)]
    load_holidays(db_conn, fetch_holidays(client_for(FakeNager()), plan), plan)
    with db_conn.cursor() as cur:
        cur.execute("SELECT * FROM raw_enrichment.public_holidays LIMIT 0")
        assert tuple(d.name for d in cur.description) == holidays.COLUMNS


def test_holiday_load_leaves_drifted_fx_rates_and_audit_alone(database_url, db_conn, monkeypatch):
    with db_conn.cursor() as cur:
        cur.execute("CREATE SCHEMA raw_enrichment")
        cur.execute("CREATE TABLE raw_enrichment.fx_rates (rate_date date, rate double precision)")
        cur.execute("INSERT INTO raw_enrichment.fx_rates VALUES ('2017-01-02', 3.4)")
    plan = [("BR", 2017)]
    load_holidays(db_conn, fetch_holidays(client_for(FakeNager()), plan), plan)
    with db_conn.cursor() as cur:
        cur.execute("SELECT * FROM raw_enrichment.fx_rates")
        assert cur.fetchall() == [(date(2017, 1, 2), 3.4)]
        # Now drift the audit table: the holiday loader exits 1 and keeps the history.
        cur.execute("ALTER TABLE raw_enrichment._load_audit ADD COLUMN extra text")
    monkeypatch.setenv("DATABASE_URL", database_url)
    assert holidays.main(["--only", "BR:2018"], client=client_for(FakeNager())) == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT table_name FROM raw_enrichment._load_audit")
        assert cur.fetchall() == [("public_holidays",)]
        cur.execute(
            "SELECT count(*) FROM raw_enrichment.public_holidays WHERE holiday_date >= '2018-01-01'"
        )
        assert cur.fetchone() == (0,)
