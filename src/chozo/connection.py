"""URL handling for both backends chozo supports.

PostgreSQL still goes through libpq: psycopg needs decoded percent-encoded
components and SQLAlchemy's `make_url` handles that decoding. SQLite uses
file paths (or `:memory:`) and never reaches `to_libpq`. The active backend is
chosen by URL scheme (see `is_sqlite`), so the rest of the codebase keeps a
single connection code path per env without leaking raw URLs into logs.
"""

from __future__ import annotations

from psycopg.conninfo import make_conninfo
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

_DEFAULT_PG_PORT = 5432


def is_sqlite(url: str) -> bool:
    """True when the URL selects the SQLite backend (any `sqlite*` drivername)."""
    try:
        return make_url(url).drivername.startswith("sqlite")
    except ArgumentError:
        return False


def describe(url: str) -> dict[str, str | int | None]:
    parsed = make_url(url)
    if parsed.drivername.startswith("sqlite"):
        return {
            "username": None,
            "host": None,
            "port": None,
            "database": parsed.database or ":memory:",
        }
    return {
        "username": parsed.username,
        "host": parsed.host,
        "port": parsed.port or _DEFAULT_PG_PORT,
        "database": parsed.database,
    }


def format_target(url: str) -> str:
    """Human label for the env badge. Never returns credentials."""
    try:
        d = describe(url)
    except ArgumentError:
        return "<unparseable database URL>"
    if d["host"] is None and d["port"] is None:
        # SQLite target is a file path (or ":memory:"); there are no credentials.
        return f"sqlite:{d['database']}"
    return f"{d['username']}@{d['host']}:{d['port']}/{d['database']}"


def to_libpq(url: str) -> str:
    """Render a SQLAlchemy PostgreSQL URL as a libpq key=value connection string.

    Only valid for PostgreSQL URLs; SQLite connections do not use libpq.
    """
    parsed = make_url(url)
    params: dict[str, str] = {
        "host": parsed.host or "",
        "port": str(parsed.port or _DEFAULT_PG_PORT),
        "dbname": parsed.database or "",
        "user": parsed.username or "",
    }
    if parsed.password is not None:
        params["password"] = parsed.password
    # Preserve common libpq query parameters such as sslmode and connect_timeout.
    for key, value in parsed.query.items():
        if isinstance(value, str):
            params[key] = value
    return make_conninfo(**params)
