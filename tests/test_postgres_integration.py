"""Opt-in execution tests restricted to a loopback PostgreSQL instance."""

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.engine import make_url

from chozo import connection, migrator
from chozo.discovery import Migration


def _local_test_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    host = make_url(url).host
    if host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.skip("integration migrations are restricted to loopback PostgreSQL")
    return url


def _migration(tmp_path: Path, table: str) -> Migration:
    directory = tmp_path / "001_integration"
    directory.mkdir()
    up = directory / "up.sql"
    down = directory / "down.sql"
    up.write_text(f'CREATE TABLE "{table}" (id integer PRIMARY KEY);\n')
    down.write_text(f'DROP TABLE "{table}";\n')
    return Migration("001_integration", 1, directory, up, down)


def _exists(url: str, table: str) -> bool:
    with psycopg.connect(connection.to_libpq(url)) as conn:
        value = conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()
    return bool(value and value[0])


@pytest.mark.dbtest
def test_apply_and_rollback_against_local_postgres(tmp_path: Path) -> None:
    url = _local_test_url()
    table = f"chozo_test_{uuid.uuid4().hex}"
    migration = _migration(tmp_path, table)
    try:
        assert migrator.execute(url, migration).status == "applied"
        assert _exists(url, table)
        assert migrator.execute_down(url, migration).status == "applied"
        assert not _exists(url, table)
    finally:
        with psycopg.connect(connection.to_libpq(url)) as conn:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')  # type: ignore[reportArgumentType]
            conn.commit()


@pytest.mark.dbtest
def test_dry_run_leaves_no_table_in_local_postgres(tmp_path: Path) -> None:
    url = _local_test_url()
    table = f"chozo_test_{uuid.uuid4().hex}"
    migration = _migration(tmp_path, table)

    assert migrator.execute(url, migration, dry_run=True).status == "applied"
    assert not _exists(url, table)
