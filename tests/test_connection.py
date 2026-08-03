"""Connection URL rendering and credential redaction."""

from chozo import connection


def test_format_target_redacts_password() -> None:
    target = connection.format_target("postgresql://alice:secret@db.example:5433/app")
    assert target == "alice@db.example:5433/app"
    assert "secret" not in target


def test_format_target_does_not_echo_invalid_url() -> None:
    target = connection.format_target("not a url with secret")
    assert target == "<unparseable database URL>"
    assert "secret" not in target


def test_to_libpq_quotes_credentials_and_preserves_options() -> None:
    conninfo = connection.to_libpq(
        "postgresql://alice:p%20a%27ss@db.example:5433/app?sslmode=require&connect_timeout=4"
    )
    assert "password='p a\\'ss'" in conninfo
    assert "sslmode=require" in conninfo
    assert "connect_timeout=4" in conninfo
