"""Split WAREHOUSE_DATABASE_URL into the dbt profile variables, masking every part first (task 2.6).

Runs as one step of `.github/workflows/warehouse-load.yml`. The loaders take the URL as
``DATABASE_URL``; dbt's postgres profile takes separate fields (warehouse/profiles.yml), so this
script parses the URL (ADR-014: in Python, no shell parsing, no ``set -x``) and:

1. prints ``::add-mask::`` for every derived secret part: user, password (percent-encoded **and**
   decoded), host, the host's first label (the Neon endpoint id, which error messages may quote on
   its own) and the database name;
2. only then appends ``DBT_PG_HOST``, ``DBT_PG_PORT``, ``DBT_PG_USER``,
   ``DBT_ENV_SECRET_PG_PASSWORD``, ``DBT_PG_DBNAME``, ``DBT_PG_SSLMODE`` and
   ``DBT_PG_CHANNEL_BINDING`` to ``$GITHUB_ENV`` (never to job or step outputs).

It never prints a value otherwise: error messages name the part or the query key, never a value.

``channel_binding``: dbt-postgres has no profile field for it, but psycopg2's bundled libpq (17 in
psycopg2-binary 2.9.13) reads ``PGCHANNELBINDING`` for any option the caller leaves unset. The
workflow exports ``PGCHANNELBINDING="$DBT_PG_CHANNEL_BINDING"`` in the dbt step only (ADR-017).
The value is always one of libpq's three keywords: ``prefer`` (libpq's default) when the URL has
none, because an *empty* ``PGCHANNELBINDING`` is an invalid value for libpq.

Exit codes: 0 ok, 2 configuration error (variable missing, URL malformed or not allowed).
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlsplit

URL_ENV = "WAREHOUSE_DATABASE_URL"
EXIT_OK = 0
EXIT_CONFIG = 2

SCHEMES = ("postgresql", "postgres")
# The warehouse connection must be encrypted (ADR-014: sslmode=require). verify-ca/verify-full
# would need a root certificate the profile does not pass (no sslrootcert field mapped), so they
# are refused with a clear message instead of failing later inside dbt.
ALLOWED_SSLMODES = ("require",)
# libpq keywords: safe to echo in an error message (anything else is not shown).
LIBPQ_SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
CHANNEL_BINDINGS = ("disable", "prefer", "require")
DEFAULT_CHANNEL_BINDING = "prefer"
ALLOWED_QUERY_KEYS = ("sslmode", "channel_binding")
MIN_LABEL_MASK = 6


class ConfigError(ValueError):
    """The URL is missing, malformed or uses an option the workflow does not support."""


@dataclass(frozen=True)
class WarehouseParts:
    user: str
    password: str
    password_encoded: str
    host: str
    port: int
    dbname: str
    sslmode: str
    channel_binding: str
    # As written in the URL (percent-encoded user/dbname, original-case host): the loaders pass
    # the URL itself to libpq, so its messages may quote these forms.
    raw_forms: tuple[str, ...] = ()

    def secrets(self) -> list[str]:
        """Every value that must be masked, longest first, without duplicates or empties."""
        hosts = {self.host, *(f for f in self.raw_forms if f.lower() == self.host)}
        # The first label of a DNS name (Neon: ep-<words>-<id>). Skipped when short or numeric
        # (an IP's "10" would mask every "10" in the log without protecting anything).
        endpoint_ids = {
            label
            for h in hosts
            if "." in h
            for label in [h.split(".", 1)[0]]
            if len(label) >= MIN_LABEL_MASK and not label.isdigit()
        }
        values = {
            self.password_encoded,
            self.password,
            self.user,
            self.dbname,
            *self.raw_forms,
            *hosts,
            *endpoint_ids,
        }
        return sorted((v for v in values if v), key=lambda v: (-len(v), v))

    def env(self) -> dict[str, str]:
        return {
            "DBT_PG_HOST": self.host,
            "DBT_PG_PORT": str(self.port),
            "DBT_PG_USER": self.user,
            "DBT_ENV_SECRET_PG_PASSWORD": self.password,
            "DBT_PG_DBNAME": self.dbname,
            "DBT_PG_SSLMODE": self.sslmode,
            "DBT_PG_CHANNEL_BINDING": self.channel_binding,
        }


def _check_printable(name: str, value: str) -> None:
    # A control character (e.g. a %0A in the password) would break both ::add-mask:: (one line per
    # command) and $GITHUB_ENV, so refuse it rather than risk an unmasked fragment.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ConfigError(f"the {name} contains a control character")


def parse_url(url: str) -> WarehouseParts:
    """Parse a PostgreSQL URL. Raises ``ConfigError`` with a message that contains no value."""
    url = url.strip()
    if not url:
        raise ConfigError(f"{URL_ENV} is not set")
    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError if not a number / out of range
    except ValueError as exc:
        raise ConfigError("the URL is malformed (bad port or brackets)") from exc
    if parts.scheme not in SCHEMES:
        raise ConfigError("the URL scheme must be postgresql:// or postgres://")
    if parts.fragment:
        raise ConfigError("the URL has a #fragment")
    netloc = parts.netloc
    if "@" not in netloc:
        raise ConfigError("the URL has no user/password (expected user:password@host)")
    userinfo, hostport = netloc.rsplit("@", 1)
    if "," in hostport:
        raise ConfigError("multi-host URLs are not supported")
    if ":" not in userinfo:
        raise ConfigError("the URL has no password")
    user_enc, password_enc = userinfo.split(":", 1)
    user = unquote(user_enc)
    password = unquote(password_enc)
    host = parts.hostname or ""  # lower-cased, brackets stripped
    host_raw = hostport.rsplit(":", 1)[0] if not hostport.endswith("]") else hostport
    host_raw = host_raw.strip("[]")
    dbname_enc = parts.path[1:] if parts.path.startswith("/") else ""
    dbname = unquote(dbname_enc)
    required = (("user", user), ("password", password), ("host", host), ("database name", dbname))
    for name, value in required:
        if not value:
            raise ConfigError(f"the URL has no {name}")
        _check_printable(name, value)
    for name, value in (("password", password_enc), ("host", host_raw)):
        _check_printable(name, value)
    if "/" in dbname:
        raise ConfigError("the database name contains '/'")

    query: dict[str, str] = {}
    for key, value in parse_qsl(parts.query, keep_blank_values=True, strict_parsing=False):
        if key not in ALLOWED_QUERY_KEYS:
            # Key names are not secret; values might be, so they are never echoed.
            raise ConfigError(
                f"unsupported URL parameter {key!r} (allowed: {', '.join(ALLOWED_QUERY_KEYS)}); "
                "dbt would connect differently from the loaders"
            )
        if key in query:
            raise ConfigError(f"URL parameter {key!r} is given twice")
        query[key] = value
    sslmode = query.get("sslmode", "")
    if sslmode not in ALLOWED_SSLMODES:
        shown = repr(sslmode) if sslmode in LIBPQ_SSLMODES else "missing or unknown"
        raise ConfigError(
            f"sslmode must be {' or '.join(ALLOWED_SSLMODES)} (got {shown}); "
            "use the Neon connection string with ?sslmode=require"
        )
    channel_binding = query.get("channel_binding", DEFAULT_CHANNEL_BINDING)
    if channel_binding not in CHANNEL_BINDINGS:
        raise ConfigError(f"channel_binding must be one of {', '.join(CHANNEL_BINDINGS)}")
    return WarehouseParts(
        user=user,
        password=password,
        password_encoded=password_enc,
        host=host,
        port=port or 5432,
        dbname=dbname,
        sslmode=sslmode,
        channel_binding=channel_binding,
        raw_forms=(user_enc, dbname_enc, host_raw),
    )


def escape_command_data(value: str) -> str:
    """Escape a value for a workflow command, as the runner unescapes ``%25``, ``%0D``, ``%0A``."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def mask_commands(parts: WarehouseParts) -> list[str]:
    """``::add-mask::`` lines for every secret part.

    The runner unescapes ``%25`` in command data, so a value containing ``%`` (e.g. the
    percent-encoded password) is sent escaped. The raw form is sent as well: if the runner did not
    unescape, the raw line registers the exact value; if it does, the raw line only adds a
    harmless extra mask.
    """
    lines: list[str] = []
    for value in parts.secrets():
        escaped = escape_command_data(value)
        lines.append(f"::add-mask::{escaped}")
        if escaped != value:
            lines.append(f"::add-mask::{value}")
    return lines


def env_file_block(env: dict[str, str], new_delimiter: Callable[[], str] | None = None) -> str:
    """``$GITHUB_ENV`` text, one heredoc block per variable with a delimiter not found in it."""
    make = new_delimiter or (lambda: f"EOF_{uuid.uuid4().hex}")
    chunks = []
    for key, value in env.items():
        delimiter = make()
        while delimiter in value or delimiter in key:
            delimiter = make()
        chunks.append(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
    return "".join(chunks)


def main(environ: dict[str, str] | None = None, out=None) -> int:
    environ = os.environ if environ is None else environ
    out = sys.stdout if out is None else out
    try:
        parts = parse_url(environ.get(URL_ENV, ""))
    except ConfigError as exc:
        print(f"::error title=Warehouse URL::{URL_ENV}: {exc}", file=out)
        return EXIT_CONFIG
    env_path = environ.get("GITHUB_ENV", "")
    if not env_path:
        print(
            "::error title=Warehouse URL::GITHUB_ENV is not set (run in GitHub Actions)", file=out
        )
        return EXIT_CONFIG
    # Masks first, flushed, before any value is written anywhere.
    for line in mask_commands(parts):
        print(line, file=out)
    out.flush()
    with open(env_path, "a", encoding="utf-8") as fh:
        fh.write(env_file_block(parts.env()))
    print(
        f"dbt profile variables exported ({len(parts.env())}); sslmode={parts.sslmode}, "
        f"channel_binding={parts.channel_binding}, port={parts.port}",
        file=out,
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
