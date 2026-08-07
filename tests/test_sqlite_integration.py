"""Real-file SQLite execution tests.

SQLite ships in the stdlib and needs no server, so these run on every machine
(unlike the PostgreSQL `dbtest` suite). They prove the SQLite backend mirrors
the PostgreSQL execution contract: apply persists, dry-run rolls back, a
failed multi-statement migration rolls back atomically, destructive ops are
blocked before connect, foreign keys are enforced, and `inspect` reflects the
file's schema.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from chozo import migrator
from chozo.discovery import Migration


def _db_url(tmp_path: Path) -> str:
    db = tmp_path / f"chozo_{uuid.uuid4().hex}.db"
    return f"sqlite:///{db}"


def _db_path(url: str) -> str:
    return make_url(url).database or ":memory:"


def _migration(tmp_path: Path, name: str, up: str, down: str | None = None) -> Migration:
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    up_path = directory / "up.sql"
    up_path.write_text(up)
    down_path = None
    if down is not None:
        down_path = directory / "down.sql"
        down_path.write_text(down)
    return Migration(name=name, number=int(name.split("_", 1)[0]), dir=directory, up=up_path, down=down_path)


def _table_exists(url: str, table: str) -> bool:
    conn = sqlite3.connect(_db_path(url))
    try:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return row is not None
    finally:
        conn.close()


@pytest.mark.parametrize("scheme", ["sqlite:///", "sqlite+pysqlite:///"])
def test_apply_and_rollback_against_sqlite_file(tmp_path: Path, scheme: str) -> None:
    url = scheme + str(tmp_path / "chozo_one.db")
    migration = _migration(
        tmp_path, "001_create", "CREATE TABLE widgets (id INTEGER PRIMARY KEY);\n", "DROP TABLE widgets;\n"
    )

    assert migrator.execute(url, migration).status == "applied"
    assert _table_exists(url, "widgets")

    assert migrator.execute_down(url, migration).status == "applied"
    assert not _table_exists(url, "widgets")


def test_dry_run_leaves_no_table_in_sqlite(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    migration = _migration(tmp_path, "001_dry", "CREATE TABLE maybe (id INTEGER);\n", "DROP TABLE maybe;\n")

    assert migrator.execute(url, migration, dry_run=True).status == "applied"
    assert not _table_exists(url, "maybe")
    # The file may exist (sqlite3.connect created it) but holds no tables.
    conn = sqlite3.connect(_db_path(url))
    try:
        assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        conn.close()


def test_sqlite_dry_run_rolls_back_dml_too(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    setup = _migration(tmp_path, "001_setup", "CREATE TABLE t (id INTEGER PRIMARY KEY);\n", "DROP TABLE t;\n")
    assert migrator.execute(url, setup).status == "applied"

    inject = _migration(
        tmp_path,
        "002_inject",
        "INSERT INTO t (id) VALUES (1);\nINSERT INTO t (id) VALUES (2);\n",
        None,
    )
    assert migrator.execute(url, inject, dry_run=True).status == "applied"

    conn = sqlite3.connect(_db_path(url))
    try:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    finally:
        conn.close()


def test_destructive_up_is_blocked_before_connect_for_sqlite(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    migration = _migration(tmp_path, "001_drop", "DROP TABLE legacy;\n", down=None)

    result = migrator.execute(url, migration)
    assert result.status == "blocked"
    assert "destructive" in (result.error or "")
    # The DB file was never created because the safety layer runs before connect.
    assert not Path(_db_path(url)).exists()


def test_failed_multi_statement_migration_rolls_back_atomically(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    migration = _migration(
        tmp_path,
        "001_partial",
        "CREATE TABLE partial (id INTEGER);\nINSERT INTO this_is_not_valid;\n",
        down=None,
    )

    result = migrator.execute(url, migration)
    assert result.status == "failed"
    assert not _table_exists(url, "partial")


def test_down_without_down_sql_is_blocked_for_sqlite(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    migration = _migration(tmp_path, "001_oneway", "CREATE TABLE one (id INTEGER);\n", down=None)
    assert migrator.execute(url, migration).status == "applied"

    result = migrator.execute_down(url, migration)
    assert result.status == "blocked"
    assert "no down.sql" in (result.error or "")
    assert _table_exists(url, "one")


def test_foreign_keys_are_enforced_on_sqlite(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    migration = _migration(
        tmp_path,
        "001_fk",
        (
            "CREATE TABLE parent (id INTEGER PRIMARY KEY);\n"
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id));\n"
            "INSERT INTO child (id, parent_id) VALUES (1, 999);\n"
        ),
        down=None,
    )

    result = migrator.execute(url, migration)
    # FK pragma is on, so the dangling insert fails and the whole migration rolls back.
    assert result.status == "failed"
    assert not _table_exists(url, "child")
    assert not _table_exists(url, "parent")


def test_reflect_returns_sqlite_schema(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    migration = _migration(
        tmp_path,
        "001_schema",
        (
            "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL);\n"
            "CREATE UNIQUE INDEX users_email ON users (email);\n"
        ),
        down=None,
    )
    assert migrator.execute(url, migration).status == "applied"

    schema = migrator.reflect(url)
    assert "users" in schema["tables"]
    users = schema["tables"]["users"]
    assert [c["name"] for c in users["columns"]] == ["id", "email"]
    id_col = users["columns"][0]
    assert id_col["primary_key"] is True
    assert users["primary_key"] == ["id"]
    assert any(idx["name"] == "users_email" and idx["unique"] for idx in users["indexes"])


def test_cli_run_and_inspect_against_sqlite_file(tmp_path: Path, monkeypatch, capsys) -> None:
    # End-to-end via the CLI with the real SQLite backend (no monkeypatching).
    from chozo.cli import main

    root = tmp_path / "app"
    (root / "migrations").mkdir(parents=True)
    (root / "migrations" / "001_init").mkdir()
    (root / "migrations" / "001_init" / "up.sql").write_text("CREATE TABLE accounts (id INTEGER PRIMARY KEY);\n")
    (root / "migrations" / "001_init" / "down.sql").write_text("DROP TABLE accounts;\n")
    db = root / "app.db"
    monkeypatch.chdir(root)
    monkeypatch.setenv("CHOZO_HOME", str(tmp_path / "chozo-home"))
    monkeypatch.delenv("GCS_MIGRATIONS_BUCKET", raising=False)
    monkeypatch.setenv("DATABASE_URL_LOCAL", f"sqlite:///{db}")

    with pytest.raises(SystemExit) as exc:
        main(["run", "all", "--env", "local", "--yes", "--json"])
    assert exc.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "success"
    assert payload["summary"]["applied"] == 1

    assert _table_exists(f"sqlite:///{db}", "accounts")

    with pytest.raises(SystemExit) as exc:
        main(["inspect", "--env", "local", "--json"])
    assert exc.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "accounts" in payload["schema"]["tables"]

    # Roll back via the CLI, proving history + DOWN both work end-to-end.
    with pytest.raises(SystemExit) as exc:
        main(["rollback", "--env", "local", "--yes", "--json"])
    assert exc.value.code == 0
    assert not _table_exists(f"sqlite:///{db}", "accounts")
