"""Migration execution behavior with a fake psycopg connection."""

from pathlib import Path

from chozo import migrator
from chozo.discovery import Migration


class FakeConnection:
    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, script: str) -> None:
        self.scripts.append(script)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _migration(tmp_path: Path, up: str, down: str | None = None) -> Migration:
    directory = tmp_path / "001_change"
    directory.mkdir()
    up_path = directory / "up.sql"
    up_path.write_text(up)
    down_path = None
    if down is not None:
        down_path = directory / "down.sql"
        down_path.write_text(down)
    return Migration("001_change", 1, directory, up_path, down_path)


def test_execute_strips_transaction_control_and_commits(tmp_path: Path, monkeypatch) -> None:
    migration = _migration(tmp_path, "BEGIN;\nCREATE TABLE example (id int);\nCOMMIT;\n")
    connection = FakeConnection()
    monkeypatch.setattr(migrator.psycopg, "connect", lambda _url: connection)

    result = migrator.execute("postgresql://unused", migration)

    assert result.status == "applied"
    assert "BEGIN" not in connection.scripts[0]
    assert "COMMIT" not in connection.scripts[0]
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_dry_run_rolls_back_and_does_not_block_destructive_sql(tmp_path: Path, monkeypatch) -> None:
    migration = _migration(tmp_path, "DROP TABLE example;")
    connection = FakeConnection()
    monkeypatch.setattr(migrator.psycopg, "connect", lambda _url: connection)

    result = migrator.execute("postgresql://unused", migration, dry_run=True)

    assert result.status == "applied"
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_destructive_up_is_blocked_before_connect(tmp_path: Path, monkeypatch) -> None:
    migration = _migration(tmp_path, "DROP TABLE example;")
    calls: list[str] = []
    monkeypatch.setattr(migrator.psycopg, "connect", lambda url: calls.append(url))

    result = migrator.execute("postgresql://unused", migration)

    assert result.status == "blocked"
    assert calls == []


def test_down_requires_down_file(tmp_path: Path) -> None:
    migration = _migration(tmp_path, "SELECT 1;")
    result = migrator.execute_down("postgresql://unused", migration)
    assert result.status == "blocked"
    assert "no down.sql" in (result.error or "")


def test_down_executes_and_rolls_back_in_dry_run(tmp_path: Path, monkeypatch) -> None:
    migration = _migration(tmp_path, "SELECT 1;", "DROP TABLE example;")
    connection = FakeConnection()
    monkeypatch.setattr(migrator.psycopg, "connect", lambda _url: connection)

    result = migrator.execute_down("postgresql://unused", migration, dry_run=True)

    assert result.status == "applied"
    assert connection.scripts == ["DROP TABLE example;"]
    assert connection.rollbacks == 1
