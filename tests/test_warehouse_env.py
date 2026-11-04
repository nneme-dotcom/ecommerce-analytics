"""Tests for scripts/warehouse_env.py (fake URLs only; nothing here is a real credential)."""

import io

import pytest

from scripts import warehouse_env as we

FAKE_HOST = "ep-fake-endpoint-123456.eu-central-1.aws.neon.tech"
FAKE = f"postgresql://fake_owner:fakePass123@{FAKE_HOST}/fakedb?sslmode=require"


def parse_env_file(text: str) -> dict[str, str]:
    """Parse $GITHUB_ENV heredoc blocks the way the runner does."""
    env: dict[str, str] = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line:
            i += 1
            continue
        key, delim = line.split("<<", 1)
        value_lines = []
        i += 1
        while lines[i] != delim:
            value_lines.append(lines[i])
            i += 1
        env[key] = "\n".join(value_lines)
        i += 1
    return env


def run_main(tmp_path, url: str | None):
    env_file = tmp_path / "github_env"
    env_file.write_text("", encoding="utf-8")
    environ = {"GITHUB_ENV": str(env_file)}
    if url is not None:
        environ[we.URL_ENV] = url
    out = io.StringIO()
    code = we.main(environ, out)
    return code, out.getvalue(), env_file.read_text(encoding="utf-8")


def masks(stdout: str) -> set[str]:
    return {
        line[len("::add-mask::") :]
        for line in stdout.splitlines()
        if line.startswith("::add-mask::")
    }


# --------------------------------------------------------------------------- parsing


def test_plain_neon_url():
    p = we.parse_url(FAKE)
    assert (p.user, p.password, p.host, p.port, p.dbname) == (
        "fake_owner",
        "fakePass123",
        FAKE_HOST,
        5432,
        "fakedb",
    )
    assert p.sslmode == "require"
    assert p.channel_binding == "prefer"  # libpq default, never empty


def test_postgres_scheme_and_explicit_port():
    p = we.parse_url(f"postgres://u:p@{FAKE_HOST}:6543/db?sslmode=require")
    assert p.port == 6543


def test_percent_encoded_password_is_decoded_and_both_forms_are_masked():
    encoded = "p%40ss%3Aw%2Fo%25rd%20x"
    p = we.parse_url(f"postgresql://fake_owner:{encoded}@{FAKE_HOST}/fakedb?sslmode=require")
    assert p.password == "p@ss:w/o%rd x"
    assert p.password_encoded == encoded
    secrets = p.secrets()
    assert encoded in secrets and "p@ss:w/o%rd x" in secrets


def test_percent_encoded_at_sign_in_password_is_accepted():
    p = we.parse_url(f"postgresql://u:pa%40ss@{FAKE_HOST}/db?sslmode=require")
    assert (p.user, p.password, p.host) == ("u", "pa@ss", FAKE_HOST)


@pytest.mark.parametrize(
    "netloc",
    [
        f"s3cr3tuser:Pw1@Pw2s3cr3t@{FAKE_HOST}",  # Python: last '@', libpq: first
        f"s3cr3tuser@x:S3cr3tPw@{FAKE_HOST}",
        f"s3cr3tuser:S3cr3tPw@@{FAKE_HOST}",
        f"s3cr3tuser:S3cr3tPw@S3cr3tPw2@{FAKE_HOST}",
    ],
)
def test_more_than_one_at_sign_is_refused_without_values(netloc):
    with pytest.raises(we.ConfigError) as info:
        we.parse_url(f"postgresql://{netloc}/db?sslmode=require")
    message = str(info.value)
    assert "more than one '@'" in message
    assert not any(bit in message.lower() for bit in ("s3cr3t", "pw1", "ep-fake", "neon"))


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://s3cr3tuser:S3cr3t Pw@{FAKE_HOST}/db?sslmode=require",  # a space
        f"postgresql://s3cr3tuser:S3cr3t\tPw@{FAKE_HOST}/db?sslmode=require",  # dropped by urlsplit
        f"postgresql://s3cr3tuser:S3cr3t\nPw@{FAKE_HOST}/db?sslmode=require",  # ... and \n
        f"postgresql://s3cr3tuser:S3cr3tPw@{FAKE_HOST}/d\rb?sslmode=require",
        f"postgresql://s3cr3tuser:S3cr3tPw@{FAKE_HOST}/db?sslmode=require\x00",
        f"postgresql://s3cr3tuser:S3cr3t\x7fPw@{FAKE_HOST}/db?sslmode=require",
        f"postgresql://s3cr3tuser:S3cr3t\x85Pw@{FAKE_HOST}/db?sslmode=require",  # C1 control (NEL)
        f"postgresql://s3cr3tuser:S3cr3t\u00a0Pw@{FAKE_HOST}/db?sslmode=require",  # no-break space
        f"postgresql://s3cr3tuser:S3cr3tPw@{FAKE_HOST}/s3cr3tdb ?sslmode=require",
    ],
)
def test_raw_whitespace_or_control_characters_are_refused_before_parsing(url):
    with pytest.raises(we.ConfigError) as info:
        we.parse_url(url)
    message = str(info.value)
    assert "whitespace" in message or "control character" in message
    assert not any(bit in message.lower() for bit in ("s3cr3t", "ep-fake", "neon"))


def test_surrounding_whitespace_is_stripped_like_the_loaders():
    assert we.parse_url(f"  {FAKE}\n").dbname == "fakedb"


def test_port_zero_is_refused():
    with pytest.raises(we.ConfigError, match="between 1 and 65535"):
        we.parse_url(f"postgresql://u:p@{FAKE_HOST}:0/db?sslmode=require")


def test_explicit_default_port_is_kept():
    assert we.parse_url(f"postgresql://u:p@{FAKE_HOST}:5432/db?sslmode=require").port == 5432


def test_percent_encoded_user_and_dbname_are_decoded_and_both_forms_masked():
    p = we.parse_url(f"postgresql://us%2Ber:pw@{FAKE_HOST}/my%20db?sslmode=require")
    assert (p.user, p.dbname) == ("us+er", "my db")
    assert {"us%2Ber", "us+er", "my%20db", "my db"} <= set(p.secrets())


def test_channel_binding_require_is_kept():
    p = we.parse_url(FAKE + "&channel_binding=require")
    assert p.channel_binding == "require"
    assert p.env()["DBT_PG_CHANNEL_BINDING"] == "require"


def test_channel_binding_first_then_sslmode():
    p = we.parse_url(f"postgresql://u:p@{FAKE_HOST}/db?channel_binding=disable&sslmode=require")
    assert (p.sslmode, p.channel_binding) == ("require", "disable")


def test_host_is_masked_with_its_endpoint_id_and_original_case():
    p = we.parse_url("postgresql://u:p@EP-Fake-Endpoint-1.example.com/db?sslmode=require")
    assert p.host == "ep-fake-endpoint-1.example.com"
    s = set(p.secrets())
    assert {"ep-fake-endpoint-1.example.com", "EP-Fake-Endpoint-1.example.com"} <= s
    assert {"ep-fake-endpoint-1", "EP-Fake-Endpoint-1"} <= s


def test_short_or_numeric_first_labels_are_not_masked_on_their_own():
    p = we.parse_url("postgresql://u:p@10.0.0.1/db?sslmode=require")
    assert "10" not in p.secrets()
    assert "10.0.0.1" in p.secrets()


def test_ipv6_host():
    p = we.parse_url("postgresql://u:p@[fd00::1]:5433/db?sslmode=require")
    assert (p.host, p.port) == ("fd00::1", 5433)


def test_secrets_are_unique_longest_first_and_never_empty():
    s = we.parse_url(FAKE).secrets()
    assert len(s) == len(set(s))
    assert all(s)
    assert [len(x) for x in s] == sorted((len(x) for x in s), reverse=True)


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("", "is not set"),
        ("   ", "is not set"),
        ("mysql://u:p@h/db?sslmode=require", "scheme"),
        (f"postgresql://{FAKE_HOST}/db?sslmode=require", "no user/password"),
        (f"postgresql://u@{FAKE_HOST}/db?sslmode=require", "no password"),
        (f"postgresql://u:@{FAKE_HOST}/db?sslmode=require", "no password"),
        (f"postgresql://:p@{FAKE_HOST}/db?sslmode=require", "no user"),
        ("postgresql://u:p@/db?sslmode=require", "no host"),
        (f"postgresql://u:p@{FAKE_HOST}?sslmode=require", "no database name"),
        (f"postgresql://u:p@{FAKE_HOST}/?sslmode=require", "no database name"),
        (f"postgresql://u:p@{FAKE_HOST}/a/b?sslmode=require", "contains '/'"),
        (f"postgresql://u:p@{FAKE_HOST}:notaport/db?sslmode=require", "malformed"),
        (f"postgresql://u:p@{FAKE_HOST}:99999/db?sslmode=require", "malformed"),
        ("postgresql://u:p@h1,h2/db?sslmode=require", "multi-host"),
        (f"postgresql://u:p@{FAKE_HOST}/db", "sslmode must be require"),
        (f"postgresql://u:p@{FAKE_HOST}/db?sslmode=disable", "'disable'"),
        (f"postgresql://u:p@{FAKE_HOST}/db?sslmode=prefer", "'prefer'"),
        (f"postgresql://u:p@{FAKE_HOST}/db?sslmode=verify-full", "'verify-full'"),
        (f"postgresql://u:p@{FAKE_HOST}/db?sslmode=require&sslmode=require", "twice"),
        (f"postgresql://u:p@{FAKE_HOST}/db?sslmode=require&channel_binding=yes", "channel_binding"),
        (f"postgresql://u:p@{FAKE_HOST}/db?sslmode=require&options=x", "'options'"),
        (f"postgresql://u:p@{FAKE_HOST}/db?sslmode=require#frag", "fragment"),
        (f"postgresql://u:p%0Aq@{FAKE_HOST}/db?sslmode=require", "control character"),
        (f"postgresql://u:p%0Dq@{FAKE_HOST}/db?sslmode=require", "control character"),
        (f"postgresql://u%00:p@{FAKE_HOST}/db?sslmode=require", "control character"),
    ],
)
def test_rejected_urls(url, fragment):
    with pytest.raises(we.ConfigError, match=None) as info:
        we.parse_url(url)
    assert fragment in str(info.value)


def test_error_messages_never_contain_values():
    secret_bits = ["S3cr3tPw", "s3cr3t-host", "s3cr3tdb", "s3cr3tuser", "s3cr3tval"]
    urls = [
        "postgresql://s3cr3tuser:S3cr3tPw@s3cr3t-host.example.com/s3cr3tdb",
        "postgresql://s3cr3tuser:S3cr3tPw@s3cr3t-host.example.com/s3cr3tdb?sslmode=s3cr3tval",
        "postgresql://s3cr3tuser:S3cr3tPw@s3cr3t-host.example.com/s3cr3tdb?sslmode=require"
        "&channel_binding=s3cr3tval",
        "postgresql://s3cr3tuser:S3cr3tPw@s3cr3t-host.example.com/s3cr3tdb?sslmode=require"
        "&options=s3cr3tval",
        "postgresql://s3cr3tuser:S3cr3tPw@s3cr3t-host.example.com:s3cr3tval/s3cr3tdb",
        "postgresql://s3cr3tuser:S3cr3tPw%0A@s3cr3t-host.example.com/s3cr3tdb?sslmode=require",
    ]
    for url in urls:
        with pytest.raises(we.ConfigError) as info:
            we.parse_url(url)
        message = str(info.value)
        assert not any(bit in message for bit in secret_bits), message


# --------------------------------------------------------------------------- masking + env file


def test_escape_command_data():
    assert we.escape_command_data("a%b") == "a%25b"
    assert we.escape_command_data("a%25b") == "a%2525b"
    assert we.escape_command_data("x") == "x"


def test_mask_commands_send_escaped_and_raw_forms_for_percent_values():
    p = we.parse_url(f"postgresql://u:pa%25ss@{FAKE_HOST}/db?sslmode=require")
    lines = we.mask_commands(p)
    # encoded form "pa%25ss": escaped (runner unescapes to the literal) and raw
    assert "::add-mask::pa%2525ss" in lines
    assert "::add-mask::pa%25ss" in lines
    # decoded form "pa%ss"
    assert "::add-mask::pa%25ss" in lines and "::add-mask::pa%ss" in lines


def test_env_file_block_heredoc_round_trip_and_delimiter_collision():
    calls = iter(["EOF_x", "EOF_x", "EOF_y", "EOF_z"])
    block = we.env_file_block({"A": "has EOF_x inside", "B": "plain"}, lambda: next(calls))
    assert parse_env_file(block) == {"A": "has EOF_x inside", "B": "plain"}
    assert "A<<EOF_y\n" in block


def test_main_masks_before_writing_and_exports_profile_vars(tmp_path):
    url = (
        f"postgresql://fake_owner:p%40ss@{FAKE_HOST}/fakedb?sslmode=require&channel_binding=require"
    )
    code, stdout, env_text = run_main(tmp_path, url)
    assert code == 0
    env = parse_env_file(env_text)
    assert env == {
        "DBT_PG_HOST": FAKE_HOST,
        "DBT_PG_PORT": "5432",
        "DBT_PG_USER": "fake_owner",
        "DBT_ENV_SECRET_PG_PASSWORD": "p@ss",
        "DBT_PG_DBNAME": "fakedb",
        "DBT_PG_SSLMODE": "require",
        "DBT_PG_CHANNEL_BINDING": "require",
    }
    m = masks(stdout)
    for value in (
        "fake_owner",
        "p@ss",
        "p%2540ss",
        "p%40ss",
        FAKE_HOST,
        "ep-fake-endpoint-123456",
        "fakedb",
    ):
        assert value in m, value
    # Outside the ::add-mask:: lines, stdout carries no secret part at all.
    other = "\n".join(line for line in stdout.splitlines() if not line.startswith("::add-mask::"))
    for value in ("fake_owner", "p@ss", "p%40ss", FAKE_HOST, "ep-fake", "fakedb"):
        assert value not in other
    # Mask lines come before the confirmation line.
    lines = stdout.splitlines()
    last_mask = max(i for i, line in enumerate(lines) if line.startswith("::add-mask::"))
    assert all(not line.startswith("::add-mask::") for line in lines[last_mask + 1 :])
    assert "exported (7)" in lines[-1]


def test_main_never_uses_step_or_job_outputs(tmp_path, monkeypatch):
    out_file = tmp_path / "github_output"
    out_file.write_text("", encoding="utf-8")
    env_file = tmp_path / "github_env"
    environ = {"GITHUB_ENV": str(env_file), "GITHUB_OUTPUT": str(out_file), we.URL_ENV: FAKE}
    assert we.main(environ, io.StringIO()) == 0
    assert out_file.read_text(encoding="utf-8") == ""


def test_main_config_error_writes_nothing_and_prints_no_value(tmp_path):
    url = "postgresql://s3cr3tuser:S3cr3tPw@s3cr3t-host.example.com/s3cr3tdb?sslmode=disable"
    code, stdout, env_text = run_main(tmp_path, url)
    assert code == 2
    assert env_text == ""
    assert "::add-mask::" not in stdout
    assert "::error" in stdout
    assert not any(bit in stdout for bit in ("s3cr3t", "S3cr3t"))


@pytest.mark.parametrize(
    "key",
    [
        "x%0A::warning::injected",  # decoded newline in the key
        "x%0D%0A::add-mask::y",
        "x%250A::warning::injected",  # decodes to a literal "%0A" the runner would unescape
    ],
)
def test_main_error_message_is_escaped_into_one_command_line(tmp_path, key):
    url = f"postgresql://u:p@{FAKE_HOST}/db?sslmode=require&{key}=v"
    code, stdout, env_text = run_main(tmp_path, url)
    assert code == 2 and env_text == ""
    lines = stdout.splitlines()
    assert len(lines) == 1 and lines[0].startswith("::error title=Warehouse URL::")
    assert "unsupported URL parameter" in lines[0]
    # What the runner would show after unescaping %25/%0D/%0A: still the single message.
    shown = lines[0].replace("%0A", "\n").replace("%0D", "\r").replace("%25", "%")
    assert "\n" not in shown and "\r" not in shown


def test_error_command_escapes_the_message():
    assert we.error_command("a%0Ab\nc") == "::error title=Warehouse URL::a%250Ab%0Ac"


def test_main_missing_url(tmp_path):
    code, stdout, env_text = run_main(tmp_path, None)
    assert code == 2 and env_text == ""
    assert "WAREHOUSE_DATABASE_URL" in stdout


def test_main_without_github_env_refuses(tmp_path):
    out = io.StringIO()
    assert we.main({we.URL_ENV: FAKE}, out) == 2
    assert "::add-mask::" not in out.getvalue()
