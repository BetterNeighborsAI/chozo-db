"""Build a libpq connection string from a SQLAlchemy URL.

psycopg needs decoded percent-encoded components; SQLAlchemy's `make_url`
handles that decoding for us. This keeps a single connection code path for
every env and avoids leaking raw URLs into logs.
"""

from __future__ import annotations

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
    """Human label for the env badge. Never raises — falls back to the raw URL."""
    try:
        d = describe(url)
    except ArgumentError:
        return f"<unparseable url: {url}>"
    return f"{d['username']}@{d['host']}:{d['port']}/{d['database']}"


def to_libpq(url: str) -> str:
    """Render a SQLAlchemy URL as a libpq key=value connection string."""
    parsed = make_url(url)
    parts = [
        f"host={parsed.host}",
        f"port={parsed.port or 5432}",
        f"dbname={parsed.database}",
        f"user={parsed.username}",
    ]
    if parsed.password is not None:
        parts.append(f"password={parsed.password}")
    return " ".join(parts)
