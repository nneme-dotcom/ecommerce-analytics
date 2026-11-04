"""FX rates: mocked Frankfurter v1 (shape from its source code, see ADR-012) + throwaway DB."""

import json
import logging
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from ingestion.enrichment import fx_rates
from ingestion.enrichment.fx_rates import (
    FxRate,
    check_density,
    fetch_rates,
    load_rates,
    parse_rates,
    year_chunks,
)
from ingestion.enrichment.http import FetchError, JsonClient

QUOTES = ("BRL", "USD")
TODAY = date(2026, 11, 3)


TARGET_CLOSED = {(1, 1), (5, 1), (12, 25), (12, 26)}  # fixed TARGET holidays (Easter ignored)


def is_publication_day(day: date) -> bool:
    return day.weekday() < 5 and (day.month, day.day) not in TARGET_CLOSED and day < TODAY


def fake_series(start: date, end: date, quotes=QUOTES, bump=Decimal("0"), snap=True) -> dict:
    """A v1 interval payload: one key per publication day, deterministic rates.

    Like Frankfurter, a start that is not a publication day snaps back to the previous one
    (a request from 2024-01-01 starts on 2023-12-29).
    """
    first = start
    if snap:
        while not is_publication_day(first) and first > start - timedelta(days=10):
            first -= timedelta(days=1)
    rates = {}
    day = first
    while day <= end:
        if is_publication_day(day):
            rates[day.isoformat()] = {
                q: Decimal("3.5") + Decimal(day.toordinal() % 100) / 100 + bump + i
                for i, q in enumerate(quotes)
            }
        day += timedelta(days=1)
    keys = sorted(rates)
    return {
        "amount": Decimal("1.0"),
        "base": "EUR",
        "start_date": keys[0] if keys else None,
        "end_date": keys[-1] if keys else None,
        "rates": rates,
    }


class FakeFrankfurter:
    def __init__(self, bump=Decimal("0")):
        self.requests: list[httpx.Request] = []
        self.bump = bump

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        span = request.url.path.rsplit("/", 1)[-1]
        start, end = (date.fromisoformat(p) for p in span.split(".."))
        quotes = tuple(request.url.params["to"].split(","))
        payload = fake_series(start, end, quotes, self.bump)
        if not payload["rates"]:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, content=json.dumps(payload, default=float).encode())


def client_for(handler) -> JsonClient:
    return JsonClient(
        "https://api.test/v1",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda s: None,
    )


def test_year_chunks():
    assert year_chunks(date(2016, 9, 1), date(2018, 2, 3)) == [
        (date(2016, 9, 1), date(2016, 12, 31)),
        (date(2017, 1, 1), date(2017, 12, 31)),
        (date(2018, 1, 1), date(2018, 2, 3)),
    ]


def test_fetch_requests_one_chunk_per_year_with_v1_params():
    fake = FakeFrankfurter()
    rows = fetch_rates(client_for(fake), date(2016, 9, 1), TODAY, QUOTES, today=TODAY)
    paths = [r.url.path for r in fake.requests]
    assert paths[0] == "/v1/2016-09-01..2016-12-31"
    assert paths[-1] == "/v1/2026-01-01..2026-11-03"
    assert len(paths) == 11
    assert all(dict(r.url.params) == {"from": "EUR", "to": "BRL,USD"} for r in fake.requests)
    assert rows == sorted(rows)
    keys = [(r.rate_date, r.quote_currency) for r in rows]
    assert len(keys) == len(set(keys))  # snapped chunk starts are not duplicated
    assert min(r.rate_date for r in rows) == date(2016, 9, 1)
    assert {r.quote_currency for r in rows} == {"BRL", "USD"}
    assert all(isinstance(r.rate, Decimal) for r in rows)


def test_parse_rejects_bad_payloads():
    start, end = date(2017, 1, 1), date(2017, 1, 31)
    good = fake_series(start, end)
    with pytest.raises(FetchError, match="no 'rates'"):
        parse_rates({"base": "EUR"}, start, end, QUOTES)
    with pytest.raises(FetchError, match="unexpected base"):
        parse_rates({**good, "base": "USD"}, start, end, QUOTES)
    with pytest.raises(FetchError, match="after the requested range"):
        parse_rates({**good, "rates": {"2017-02-01": {"BRL": 3}}}, start, end, QUOTES)
    with pytest.raises(FetchError, match="unexpected amount"):
        parse_rates({**good, "amount": "abc"}, start, end, QUOTES)
    with pytest.raises(FetchError, match="not a positive number"):
        bad = {"2017-01-02": {"BRL": Decimal("NaN"), "USD": 1}}
        parse_rates({**good, "rates": bad}, start, end, QUOTES)
    with pytest.raises(FetchError, match="unrequested currency"):
        parse_rates({**good, "rates": {"2017-01-02": {"GBP": 1}}}, start, end, QUOTES)
    with pytest.raises(FetchError, match="not a positive number"):
        parse_rates({**good, "rates": {"2017-01-02": {"BRL": 0, "USD": 1}}}, start, end, QUOTES)
    with pytest.raises(FetchError, match="not a positive number"):
        bad = {"2017-01-02": {"BRL": Decimal("-1"), "USD": 1}}
        parse_rates({**good, "rates": bad}, start, end, QUOTES)
    with pytest.raises(FetchError, match="not a number"):
        parse_rates({**good, "rates": {"2017-01-02": {"BRL": "3.4"}}}, start, end, QUOTES)
    with pytest.raises(FetchError, match=r"no rates at all for \['USD'\]"):
        parse_rates(fake_series(start, end, ("BRL",)), start, end, QUOTES)


def test_start_snapped_to_previous_publication_day_is_dropped(caplog):
    start, end = date(2024, 1, 1), date(2024, 1, 31)
    payload = fake_series(start, end)
    assert payload["start_date"] == "2023-12-29"  # as Frankfurter answers
    with caplog.at_level(logging.DEBUG, logger="ingestion.enrichment.fx_rates"):
        rows = parse_rates(payload, start, end, QUOTES)
    assert min(r.rate_date for r in rows) == date(2024, 1, 2)
    assert "dropped 1 date(s) before the start: 2023-12-29" in caplog.text


def test_density_check_catches_downsampled_series():
    start, end = date(2017, 1, 1), date(2017, 12, 31)
    weekly = [FxRate(start + timedelta(weeks=i), "EUR", "BRL", Decimal(3)) for i in range(52)]
    with pytest.raises(FetchError, match="downsampled or truncated"):
        check_density(weekly, start, end, TODAY)
    # A short or not-yet-published range is not checked.
    check_density([], TODAY, TODAY, TODAY)
    check_density([], date(2026, 10, 25), TODAY, TODAY)


def test_chunk_with_404_and_nothing_else_fails():
    fake = FakeFrankfurter()
    with pytest.raises(FetchError, match="no rates returned"):
        fetch_rates(client_for(fake), TODAY, TODAY, QUOTES, today=TODAY)


def test_ars_is_refused_on_the_command_line(capsys):
    with pytest.raises(SystemExit) as exc:
        fx_rates.build_parser().parse_args(["--quotes", "BRL,ARS"])
    assert exc.value.code == 2
    assert "ECB does not publish an ARS" in capsys.readouterr().err


def test_main_csv_only_without_database(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    out = tmp_path / "fx.csv"
    code = fx_rates.main(
        ["--start", "2017-01-01", "--end", "2017-03-31", "--csv-out", str(out)],
        client=client_for(FakeFrankfurter()),
    )
    assert code == 0
    lines = out.read_text().splitlines()
    assert lines[0] == "rate_date,base_currency,quote_currency,rate,source"
    assert len(lines) == 1 + 2 * 65  # 65 weekdays in Q1 2017


def test_main_without_database_or_csv_is_config_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert fx_rates.main([], client=client_for(FakeFrankfurter())) == 2


# --- database ---------------------------------------------------------------------------------


def fx_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rate_date, quote_currency, rate FROM raw_enrichment.fx_rates ORDER BY 1, 2"
        )
        return cur.fetchall()


def test_load_is_idempotent_and_replaces_only_the_requested_range(db_conn):
    start, end = date(2017, 1, 1), date(2017, 12, 31)
    rows = fetch_rates(client_for(FakeFrankfurter()), start, end, QUOTES, today=TODAY)
    load_rates(db_conn, rows, start, end, QUOTES)
    first = fx_table(db_conn)
    assert len(first) == len(rows) == 2 * 257  # 2017: 260 weekdays, 3 TARGET-closed
    load_rates(db_conn, rows, start, end, QUOTES)
    assert fx_table(db_conn) == first

    # Re-fetch March with revised rates: March replaced, the rest untouched.
    m_start, m_end = date(2017, 3, 1), date(2017, 3, 31)
    revised = fetch_rates(
        client_for(FakeFrankfurter(bump=Decimal("1"))), m_start, m_end, QUOTES, today=TODAY
    )
    load_rates(db_conn, revised, m_start, m_end, QUOTES)
    after = fx_table(db_conn)
    assert len(after) == len(first)
    changed = [a for a, b in zip(after, first, strict=True) if a != b]
    assert {r[0].month for r in changed} == {3}
    assert all(a[2] == b[2] + 1 for a, b in zip(after, first, strict=True) if a[0].month == 3)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, row_count, params->>'start' FROM raw_enrichment._load_audit "
            "ORDER BY loaded_at"
        )
        audit = cur.fetchall()
    assert [a[0] for a in audit] == ["fx_rates"] * 3
    assert audit[-1][1:] == (len(revised), "2017-03-01")


def test_rates_keep_full_precision(db_conn):
    rows = [FxRate(date(2017, 1, 2), "EUR", "BRL", Decimal("3.4305123456789"))]
    load_rates(db_conn, rows, date(2017, 1, 2), date(2017, 1, 2), ("BRL",))
    assert fx_table(db_conn) == [(date(2017, 1, 2), "BRL", Decimal("3.4305123456789"))]


def test_main_loads_into_database(database_url, db_conn, monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", database_url)
    code = fx_rates.main(
        ["--start", "2018-01-01", "--end", "2018-01-31"], client=client_for(FakeFrankfurter())
    )
    assert code == 0
    assert len(fx_table(db_conn)) == 2 * 22  # Jan 2018: 23 weekdays minus Jan 1
    assert database_url not in caplog.text


def test_table_with_drifted_column_type_is_recreated(db_conn):
    with db_conn.cursor() as cur:
        cur.execute("CREATE SCHEMA raw_enrichment")
        cur.execute(
            "CREATE TABLE raw_enrichment.fx_rates (rate_date date, base_currency char(3),"
            " quote_currency char(3), rate double precision, source text)"
        )
    rows = [FxRate(date(2017, 1, 2), "EUR", "BRL", Decimal("3.4305123456789"))]
    load_rates(db_conn, rows, date(2017, 1, 2), date(2017, 1, 2), ("BRL",))
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT data_type FROM information_schema.columns WHERE table_schema ="
            " 'raw_enrichment' AND table_name = 'fx_rates' AND column_name = 'rate'"
        )
        assert cur.fetchone() == ("numeric",)
    assert fx_table(db_conn) == [(date(2017, 1, 2), "BRL", Decimal("3.4305123456789"))]
