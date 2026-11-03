"""Holidays: mocked Nager.Date v4 (shape from the official repo docs, ADR-012) + throwaway DB."""

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


def v4_payload(country: str, year: int) -> list[dict]:
    """Entries in the documented v4 shape (doc/examples/rest-api.md)."""
    return [
        {
            "date": f"{year}-01-01",
            "name": "New Year's Day",
            "countryCode": country,
            "nationalHoliday": True,
            "subdivisionCodes": None,
            "holidayTypes": ["Public"],
        },
        {
            "date": f"{year}-07-09",
            "name": "Regional Day",
            "countryCode": country,
            "nationalHoliday": False,
            "subdivisionCodes": [f"{country}-SP", f"{country}-RJ"],
            "holidayTypes": ["Public", "Bank"],
        },
    ]


class FakeNager:
    def __init__(self, empty: set[tuple[str, int]] = frozenset()):
        self.paths: list[str] = []
        self.empty = empty

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        _, _, _, _, country, year = request.url.path.split("/")
        if (country, int(year)) in self.empty:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=v4_payload(country, int(year)))


def client_for(handler) -> JsonClient:
    return JsonClient(
        "https://nager.test/api/v4",
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


def test_fetch_uses_v4_country_before_year():
    fake = FakeNager()
    rows = fetch_holidays(client_for(fake), [("BR", 2017), ("AR", 2026)])
    assert fake.paths == ["/api/v4/Holidays/BR/2017", "/api/v4/Holidays/AR/2026"]
    assert len(rows) == 4
    new_year = rows[0] if rows[0].country_code == "AR" else rows[2]
    assert new_year.subdivision_codes == ()  # null -> empty
    assert new_year.national_holiday is True


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: {"not": "a list"}, "expected a JSON array"),
        (lambda p: [{**p[0], "date": "2018-01-01"}], "is not in 2017"),
        (lambda p: [{**p[0], "date": None}], "invalid date"),
        (lambda p: [{**p[0], "name": ""}], "missing name"),
        (lambda p: [{**p[0], "countryCode": "AR"}], "countryCode 'AR'"),
        (lambda p: [{**p[0], "nationalHoliday": "yes"}], "not a boolean"),
        (lambda p: [{**p[0], "holidayTypes": "Public"}], "not a list of strings"),
        (lambda p: [], "no holidays returned"),
    ],
)
def test_parse_rejects_bad_payloads(mutate, message):
    with pytest.raises(FetchError, match=message):
        parse_holidays(mutate(v4_payload("BR", 2017)), "BR", 2017)


def test_missing_optional_fields_are_null_or_empty():
    [h] = parse_holidays([{"date": "2017-04-21", "name": "Tiradentes"}], "BR", 2017)
    assert (h.national_holiday, h.subdivision_codes, h.holiday_types) == (None, (), ())


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
    assert lines[1] == "BR,2017-01-01,New Year's Day,true,,Public,Nager.Date v4"
    assert lines[2].endswith(",false,BR-SP;BR-RJ,Public;Bank,Nager.Date v4")


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
            "SELECT country_code, holiday_date, name, national_holiday, subdivision_codes,"
            " holiday_types FROM raw_enrichment.public_holidays ORDER BY 1, 2, 3"
        )
        return cur.fetchall()


def test_load_is_idempotent_and_scoped_to_requested_country_years(db_conn):
    plan = [("BR", 2016), ("BR", 2017), ("AR", 2026)]
    rows = fetch_holidays(client_for(FakeNager()), plan)
    load_holidays(db_conn, rows, plan)
    first = holiday_table(db_conn)
    assert len(first) == 6
    assert first[0][4] == [] and first[1][4] == ["AR-SP", "AR-RJ"]
    load_holidays(db_conn, rows, plan)
    assert holiday_table(db_conn) == first

    # Reloading BR 2017 alone with a different answer leaves BR 2016 and AR 2026 untouched.
    single = [("BR", 2017)]
    load_holidays(db_conn, parse_holidays(v4_payload("BR", 2017)[:1], "BR", 2017), single)
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
