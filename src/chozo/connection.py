"""Build a libpq connection string from a SQLAlchemy URL.

psycopg needs decoded percent-encoded components; SQLAlchemy's `make_url`
handles that decoding for us. This keeps a single connection code path for
every env and avoids leaking raw URLs into logs.
"""

from __future__ import annotations

from psycopg.conninfo import make_conninfo
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


def describe(url: str) -> dict[str, str | int | None]:
    parsed = make_url(url)
    return {
        "username": parsed.username,
        "host": parsed.host,
        "port": parsed.port or 5432,
        "database": parsed.database,
    }


def format_target(url: str) -> str:
    """Human label for the env badge. Never returns credentials."""
    try:
        d = describe(url)
    except ArgumentError:
        return "<unparseable database URL>"
    return f"{d['username']}@{d['host']}:{d['port']}/{d['database']}"


def to_libpq(url: str) -> str:
    """Render a SQLAlchemy URL as a libpq key=value connection string."""
    parsed = make_url(url)
    params: dict[str, str] = {
        "host": parsed.host or "",
        "port": str(parsed.port or 5432),
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
