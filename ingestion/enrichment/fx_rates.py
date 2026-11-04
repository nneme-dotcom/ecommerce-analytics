"""Daily ECB euro reference rates (EUR → BRL, USD) via Frankfurter, loaded into
``raw_enrichment.fx_rates`` (DATA_CONTRACT §2b).

    DATABASE_URL=... python -m ingestion.enrichment.fx_rates [--start 2016-09-01] [--end YYYY-MM-DD]
    python -m ingestion.enrichment.fx_rates --csv-out fx.csv     # fetch only, no database

Source assumptions (docs/DECISIONS.md ADR-012; the API is not reachable from the agents' workspace,
so they come from Frankfurter's source code and changelog, not from a live call):
- Frankfurter **v1** (``https://api.frankfurter.dev/v1``; ``api.frankfurter.app`` is the pre-2024
  domain) serves only ECB data and is frozen "indefinitely". v2 blends 50+ providers, so it is not
  used: the contract promises ECB rates.
- ``GET /v1/<start>..<end>?from=EUR&to=BRL,USD`` →
  ``{"amount": 1.0, "base": "EUR", "start_date", "end_date", "rates": {"YYYY-MM-DD": {"BRL": r}}}``,
  one key per ECB publication day (no weekends/TARGET holidays). 404 = no data in the range.
  If ``<start>`` is not a publication day, the answer **starts on the previous publication day**
  (e.g. a request from 2024-01-01 returns 2023-12-29 first). Such leading dates are dropped (the
  previous chunk covers them) if at most 7 calendar days before ``<start>``; an older date, or a
  date after ``<end>``, is an error.
- **ARS is not an ECB reference currency**, so it is refused here (left for a later task).

The range is fetched one calendar year per request. Every chunk is validated (base, no date after
the range, positive rates, only requested currencies, every currency present) and must be dense
for **each** currency (≥ 80 % of weekdays), so a downsampled or truncated series fails instead of
loading silently.
Load: one transaction deletes the requested (date range × currencies) and inserts the fetched
rows, then writes an audit row: re-running is idempotent and picks up ECB revisions.
Exit codes: 0 ok · 1 fetch/validation/load failed (nothing changed) · 2 configuration error.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ingestion.db import ConfigError, connect, database_url_from_env
from ingestion.enrichment.http import FetchError, JsonClient
from ingestion.enrichment.store import SCHEMA, SchemaDriftError, ensure_schema, write_audit

FRANKFURTER_V1 = "https://api.frankfurter.dev/v1"
SOURCE = "ECB via Frankfurter v1"
BASE_CURRENCY = "EUR"
DEFAULT_START = date(2016, 9, 1)  # Olist orders start in September 2016
DEFAULT_QUOTES = ("BRL", "USD")
NOT_FROM_ECB = {"ARS": "the ECB does not publish an ARS reference rate"}
MIN_WEEKDAY_COVERAGE = 0.8
MIN_WEEKDAYS_FOR_DENSITY_CHECK = 20
MAX_START_SNAP = timedelta(days=7)  # longest TARGET closure (Good Friday..Easter Monday) is 4 days

EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2

logger = logging.getLogger(__name__)


@dataclass(frozen=True, order=True)
class FxRate:
    rate_date: date
    base_currency: str
    quote_currency: str
    rate: Decimal


def year_chunks(start: date, end: date) -> list[tuple[date, date]]:
    chunks = []
    cursor = start
    while cursor <= end:
        chunk_end = min(date(cursor.year, 12, 31), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def weekdays_between(start: date, end: date) -> int:
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(i)).weekday() < 5)


def parse_rates(payload: Any, start: date, end: date, quotes: tuple[str, ...]) -> list[FxRate]:
    if not isinstance(payload, dict) or not isinstance(payload.get("rates"), dict):
        raise FetchError("unexpected payload: no 'rates' object")
    if payload.get("base") != BASE_CURRENCY:
        raise FetchError(f"unexpected base {payload.get('base')!r}, expected {BASE_CURRENCY}")
    if "amount" in payload:
        try:
            amount_ok = Decimal(str(payload["amount"])) == 1
        except InvalidOperation:
            amount_ok = False
        if not amount_ok:
            raise FetchError(f"unexpected amount {payload['amount']!r}")
    out: list[FxRate] = []
    dropped: list[date] = []
    for day, rates in payload["rates"].items():
        try:
            rate_date = date.fromisoformat(day)
        except (TypeError, ValueError):
            raise FetchError(f"unexpected date key {day!r}") from None
        if rate_date > end:
            raise FetchError(f"date {day} after the requested range {start}..{end}")
        if rate_date < start:
            # Frankfurter snaps the start back to the previous publication day (a few days at most).
            if start - rate_date > MAX_START_SNAP:
                raise FetchError(
                    f"date {day} more than {MAX_START_SNAP.days} days before the requested range"
                    f" {start}..{end}"
                )
            dropped.append(rate_date)
            continue
        if not isinstance(rates, dict):
            raise FetchError(f"{day}: rates is not an object")
        for quote, value in rates.items():
            if quote not in quotes:
                raise FetchError(f"{day}: unrequested currency {quote!r}")
            if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
                raise FetchError(f"{day} {quote}: rate is not a number")
            try:
                rate = Decimal(value)
                positive = rate.is_finite() and rate > 0
            except InvalidOperation:
                positive = False
            if not positive:
                raise FetchError(f"{day} {quote}: rate {value!r} is not a positive number")
            out.append(FxRate(rate_date, BASE_CURRENCY, quote, rate))
    if dropped:
        logger.debug(
            "%s..%s: dropped %d date(s) before the start: %s",
            start,
            end,
            len(dropped),
            ", ".join(d.isoformat() for d in sorted(dropped)),
        )
    if out:
        missing = set(quotes) - {r.quote_currency for r in out}
        if missing:
            raise FetchError(f"{start}..{end}: no rates at all for {sorted(missing)}")
    return sorted(out)


def check_density(
    rows: list[FxRate], start: date, end: date, today: date, quotes: tuple[str, ...]
) -> None:
    """A full daily series has a rate on ~97 % of weekdays (TARGET holidays aside), per currency."""
    last_published = min(end, today - timedelta(days=1))
    if last_published < start:
        return
    weekdays = weekdays_between(start, last_published)
    if weekdays < MIN_WEEKDAYS_FOR_DENSITY_CHECK:
        return
    for quote in quotes:
        days = len({r.rate_date for r in rows if r.quote_currency == quote})
        if days < MIN_WEEKDAY_COVERAGE * weekdays:
            raise FetchError(
                f"{start}..{end}: only {days} {quote} rate days for {weekdays} weekdays "
                "(downsampled or truncated series?)"
            )


def fetch_rates(
    client: JsonClient, start: date, end: date, quotes: tuple[str, ...], *, today: date
) -> list[FxRate]:
    rows: list[FxRate] = []
    for chunk_start, chunk_end in year_chunks(start, end):
        status, payload = client.get_json(
            f"/{chunk_start.isoformat()}..{chunk_end.isoformat()}",
            {"from": BASE_CURRENCY, "to": ",".join(quotes)},
            ok_statuses=(404,),
        )
        chunk = [] if status == 404 else parse_rates(payload, chunk_start, chunk_end, quotes)
        check_density(chunk, chunk_start, chunk_end, today, quotes)
        logger.info("fx %s..%s: %d rates", chunk_start, chunk_end, len(chunk))
        rows.extend(chunk)
    if not rows:
        raise FetchError(f"no rates returned for {start}..{end}")
    return rows


def load_rates(conn, rows: list[FxRate], start: date, end: date, quotes: tuple[str, ...]) -> int:
    load_id = uuid.uuid4()
    with conn.transaction(), conn.cursor() as cur:
        ensure_schema(cur, "fx_rates")
        cur.execute(
            f"DELETE FROM {SCHEMA}.fx_rates WHERE base_currency = %s"  # noqa: S608
            " AND quote_currency = ANY(%s) AND rate_date BETWEEN %s AND %s",
            (BASE_CURRENCY, list(quotes), start, end),
        )
        with cur.copy(
            f"COPY {SCHEMA}.fx_rates (rate_date, base_currency, quote_currency, rate, source)"
            " FROM STDIN"
        ) as copy:
            for r in rows:
                copy.write_row((r.rate_date, r.base_currency, r.quote_currency, r.rate, SOURCE))
        params = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "base": BASE_CURRENCY,
            "quotes": list(quotes),
            "source": SOURCE,
        }
        write_audit(cur, load_id, "fx_rates", len(rows), params)
    return len(rows)


def write_csv(rows: list[FxRate], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rate_date", "base_currency", "quote_currency", "rate", "source"])
        for r in rows:
            writer.writerow([r.rate_date, r.base_currency, r.quote_currency, r.rate, SOURCE])


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from None


def _quotes(value: str) -> tuple[str, ...]:
    quotes = tuple(dict.fromkeys(q.strip().upper() for q in value.split(",") if q.strip()))
    for quote in quotes:
        if quote in NOT_FROM_ECB:
            raise argparse.ArgumentTypeError(f"{quote}: {NOT_FROM_ECB[quote]}")
        if len(quote) != 3 or not quote.isalpha() or quote == BASE_CURRENCY:
            raise argparse.ArgumentTypeError(f"invalid quote currency {quote!r}")
    if not quotes:
        raise argparse.ArgumentTypeError("at least one currency")
    return quotes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ingestion.enrichment.fx_rates",
        description="Load daily ECB reference rates (EUR base) into raw_enrichment.fx_rates.",
    )
    parser.add_argument("--start", type=_iso_date, default=DEFAULT_START)
    parser.add_argument("--end", type=_iso_date, help="default: today (UTC)")
    parser.add_argument("--quotes", type=_quotes, default=DEFAULT_QUOTES, help="e.g. BRL,USD")
    parser.add_argument("--base-url", default=FRANKFURTER_V1)
    parser.add_argument("--max-requests", type=int, default=40)
    parser.add_argument(
        "--csv-out", type=Path, help="also write the rows to this CSV (no DATABASE_URL needed)"
    )
    return parser


def main(argv: list[str] | None = None, *, client: JsonClient | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # our client logs path + status
    today = datetime.now(UTC).date()
    end = args.end or today
    if args.start > end:
        logger.error("--start %s is after --end %s", args.start, end)
        return EXIT_CONFIG
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
            rows = fetch_rates(client, args.start, end, args.quotes, today=today)
    except FetchError as exc:
        logger.error("fx fetch failed, nothing loaded: %s", exc)
        return EXIT_FAILED
    if args.csv_out:
        write_csv(rows, args.csv_out)
        logger.info("wrote %d rows to %s", len(rows), args.csv_out)
    if url is None:
        return EXIT_OK
    return _load(url, rows, args.start, end, args.quotes)


def _load(url: str, rows: list[FxRate], start: date, end: date, quotes: tuple[str, ...]) -> int:
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
            n = load_rates(conn, rows, start, end, quotes)
    except (psycopg.Error, SchemaDriftError) as exc:
        logger.error("fx load rolled back, nothing changed: %s", exc)
        return EXIT_FAILED
    logger.info("loaded %s.fx_rates: %d rows (%s..%s, %s)", SCHEMA, n, start, end, ",".join(quotes))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
