"""SQLite backend for local, serverless projects.

SQLite ships with Python's stdlib, so a project can run `chozo` against a local
file (or `:memory:`) with no database server. The connection wrapper here
mirrors the PostgreSQL execution contract: the runner owns the transaction and
wraps each migration in a single atomic transaction, so apply / dry-run / rollback
behave identically across backends.

Because `sqlite3` does not execute multiple statements via `Connection.execute`,
migrations run through `executescript`. With `isolation_level=None` the driver
injects no transaction control of its own, so we open an explicit `BEGIN` and
close it with `commit()` / `rollback()`. This guarantees that a failed
multi-statement migration rolls back every preceding statement (validated in the
test suite) — the same atomicity the psycopg path provides.
"""

from __future__ import annotations

import sqlite3

from sqlalchemy.engine import make_url


class SqliteConnection:
    """Minimal connection surface matching what `chozo.migrator` calls.

    Implements the same context-manager + execute/commit/rollback shape as a
    `psycopg.Connection`, so the runner stays backend-agnostic.
    """

    def __init__(self, url: str) -> None:
        database = make_url(url).database or ":memory:"
        # isolation_level=None: autocommit at the driver level, so the driver
        # never implicitly begins or commits. We wrap each migration in an
        # explicit BEGIN ... COMMIT/ROLLBACK instead.
        self._conn = sqlite3.connect(database, isolation_level=None)
        # Enforce foreign keys to match PostgreSQL semantics. Must be set outside
        # a transaction; we set it before any BEGIN, so it applies to every
        # migration executed through this connection.
        self._conn.execute("PRAGMA foreign_keys = ON;")

    def __enter__(self) -> SqliteConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._conn.close()

    def execute(self, script: str) -> None:
        # `executescript` runs the whole script in one call. BEGIN opens the
        # single transaction that commit()/rollback() will close; the runner
        # has already stripped any standalone transaction control from `script`.
        self._conn.executescript(f"BEGIN;\n{script}")

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


def connect(url: str) -> SqliteConnection:
    """Open a SQLite connection for the given SQLAlchemy-style URL."""
    return SqliteConnection(url)
