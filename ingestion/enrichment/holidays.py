"""Public holidays from Nager.Date, loaded into ``raw_enrichment.public_holidays``
(DATA_CONTRACT §2b).

    DATABASE_URL=... python -m ingestion.enrichment.holidays     # BR 2016-18, AR 2026-next year
    python -m ingestion.enrichment.holidays --only BR:2017 --csv-out h.csv    # fetch only

Source assumptions (ADR-012; the API is not reachable from the agents' workspace, so they come from
the official Nager.Date repository docs, ``doc/examples/rest-api.md``, not from a live call):
- REST API **v4**: ``GET https://nagerholidays.com/api/v4/Holidays/{countryCode}/{year}`` returns a
  JSON array of ``{date, name, countryCode, nationalHoliday, subdivisionCodes, holidayTypes}``.
  v3 (``date.nager.at/api/v3/PublicHolidays/{year}/{cc}``) reaches end of life on 2027-01-31,
  so it is not used. v4 has no ``localName`` field.
- ``subdivisionCodes`` is null or empty for national holidays (stored as ``[]``).

Every (country, year) must return at least one valid entry dated inside that year, otherwise the
run fails and nothing is loaded (an empty answer never wipes good data). Load: one transaction
deletes the requested (country, year) pairs and inserts the fetched rows, plus an audit row.
Exit codes: 0 ok · 1 fetch/validation/load failed (nothing changed) · 2 configuration error.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ingestion.db import ConfigError, connect, database_url_from_env
from ingestion.enrichment.http import FetchError, JsonClient
from ingestion.enrichment.store import SCHEMA, ensure_schema, write_audit

NAGER_V4 = "https://nagerholidays.com/api/v4"
SOURCE = "Nager.Date v4"

EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2

logger = logging.getLogger(__name__)


@dataclass(frozen=True, order=True)
class Holiday:
    country_code: str
    holiday_date: date
    name: str
    national_holiday: bool | None
    subdivision_codes: tuple[str, ...]
    holiday_types: tuple[str, ...]


def default_plan(today: date) -> list[tuple[str, int]]:
    """Brazil for the Olist years; Argentina from the first ML snapshot year to next year."""
    return [("BR", y) for y in range(2016, 2019)] + [("AR", y) for y in range(2026, today.year + 2)]


def _str_list(value: Any, field: str, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise FetchError(f"{where}: {field} is not a list of strings")
    return tuple(value)


def parse_holidays(payload: Any, country: str, year: int) -> list[Holiday]:
    where = f"{country} {year}"
    if not isinstance(payload, list):
        raise FetchError(f"{where}: expected a JSON array")
    out: list[Holiday] = []
    for i, item in enumerate(payload):
        at = f"{where} entry {i}"
        if not isinstance(item, dict):
            raise FetchError(f"{at}: not an object")
        try:
            day = date.fromisoformat(str(item.get("date"))[:10])
        except ValueError:
            raise FetchError(f"{at}: invalid date {item.get('date')!r}") from None
        if day.year != year:
            raise FetchError(f"{at}: date {day} is not in {year}")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise FetchError(f"{at}: missing name")
        code = item.get("countryCode")
        if code is not None and code != country:
            raise FetchError(f"{at}: countryCode {code!r}, expected {country}")
        national = item.get("nationalHoliday")
        if national is not None and not isinstance(national, bool):
            raise FetchError(f"{at}: nationalHoliday is not a boolean")
        out.append(
            Holiday(
                country_code=country,
                holiday_date=day,
                name=name,
                national_holiday=national,
                subdivision_codes=_str_list(item.get("subdivisionCodes"), "subdivisionCodes", at),
                holiday_types=_str_list(item.get("holidayTypes"), "holidayTypes", at),
            )
        )
    if not out:
        raise FetchError(f"{where}: no holidays returned")
    return sorted(out)


def fetch_holidays(client: JsonClient, plan: list[tuple[str, int]]) -> list[Holiday]:
    rows: list[Holiday] = []
    for country, year in plan:
        _, payload = client.get_json(f"/Holidays/{country}/{year}")
        chunk = parse_holidays(payload, country, year)
        logger.info("holidays %s %d: %d entries", country, year, len(chunk))
        rows.extend(chunk)
    return rows


def load_holidays(conn, rows: list[Holiday], plan: list[tuple[str, int]]) -> int:
    load_id = uuid.uuid4()
    with conn.transaction(), conn.cursor() as cur:
        ensure_schema(cur)
        for country, year in plan:
            cur.execute(
                f"DELETE FROM {SCHEMA}.public_holidays WHERE country_code = %s"  # noqa: S608
                " AND holiday_date BETWEEN %s AND %s",
                (country, date(year, 1, 1), date(year, 12, 31)),
            )
        with cur.copy(
            f"COPY {SCHEMA}.public_holidays (country_code, holiday_date, name, national_holiday,"
            " subdivision_codes, holiday_types, source) FROM STDIN"
        ) as copy:
            for h in rows:
                copy.write_row(
                    (
                        h.country_code,
                        h.holiday_date,
                        h.name,
                        h.national_holiday,
                        list(h.subdivision_codes),
                        list(h.holiday_types),
                        SOURCE,
                    )
                )
        params = {"countries_years": [f"{c}:{y}" for c, y in plan], "source": SOURCE}
        write_audit(cur, load_id, "public_holidays", len(rows), params)
    return len(rows)


def write_csv(rows: list[Holiday], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "country_code",
                "holiday_date",
                "name",
                "national_holiday",
                "subdivision_codes",
                "holiday_types",
                "source",
            ]
        )
        for h in rows:
            writer.writerow(
                [
                    h.country_code,
                    h.holiday_date,
                    h.name,
                    "" if h.national_holiday is None else str(h.national_holiday).lower(),
                    ";".join(h.subdivision_codes),
                    ";".join(h.holiday_types),
                    SOURCE,
                ]
            )


_ONLY = re.compile(r"^([A-Z]{2}):(\d{4})(?:-(\d{4}))?$")


def _only(value: str) -> list[tuple[str, int]]:
    match = _ONLY.match(value.strip().upper())
    if not match:
        raise argparse.ArgumentTypeError("expected CC:YYYY or CC:YYYY-YYYY (e.g. BR:2016-2018)")
    first = int(match.group(2))
    last = int(match.group(3) or first)
    if last < first or last - first > 20:
        raise argparse.ArgumentTypeError("invalid year range")
    return [(match.group(1), y) for y in range(first, last + 1)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ingestion.enrichment.holidays",
        description="Load public holidays (Nager.Date) into raw_enrichment.public_holidays.",
    )
    parser.add_argument(
        "--only",
        type=_only,
        action="append",
        help="country:years to fetch instead of the default plan (repeatable)",
    )
    parser.add_argument("--base-url", default=NAGER_V4)
    parser.add_argument("--max-requests", type=int, default=40)
    parser.add_argument(
        "--csv-out", type=Path, help="also write the rows to this CSV (no DATABASE_URL needed)"
    )
    return parser


def main(argv: list[str] | None = None, *, client: JsonClient | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # our client logs path + status
    if args.only:
        plan = list(dict.fromkeys(pair for group in args.only for pair in group))
    else:
        plan = default_plan(datetime.now(UTC).date())
    url = None
    try:
        url = database_url_from_env()
    except ConfigError as exc:
        if not args.csv_out:
            logger.error("%s (or pass --csv-out to fetch without loading)", exc)
            return EXIT_CONFIG
    client = client or JsonClient(args.base_url, max_requests=args.max_requests)
    try:
        with client:
            rows = fetch_holidays(client, plan)
    except FetchError as exc:
        logger.error("holiday fetch failed, nothing loaded: %s", exc)
        return EXIT_FAILED
    if args.csv_out:
        write_csv(rows, args.csv_out)
        logger.info("wrote %d rows to %s", len(rows), args.csv_out)
    if url is None:
        return EXIT_OK

    import psycopg

    try:
        conn = connect(url)
    except psycopg.Error as exc:
        logger.error(
            "could not connect to the database (%s); check DATABASE_URL", type(exc).__name__
        )
        return EXIT_FAILED
    try:
        with conn:
            n = load_holidays(conn, rows, plan)
    except psycopg.Error as exc:
        logger.error("holiday load rolled back, nothing changed: %s", exc)
        return EXIT_FAILED
    logger.info("loaded %s.public_holidays: %d rows for %d country-years", SCHEMA, n, len(plan))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
