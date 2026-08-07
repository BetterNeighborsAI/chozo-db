"""CLI contracts for JSON mode and migracli-compatible execution."""

import json
from pathlib import Path

import pytest

from chozo import migrator
from chozo.cli import build_parser, main
from chozo.history import FileHistoryStore


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "app"
    migrations = root / "migrations"
    migrations.mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.setenv("CHOZO_HOME", str(tmp_path / "chozo-home"))
    monkeypatch.delenv("GCS_MIGRATIONS_BUCKET", raising=False)
    return root


def _flat(project: Path, name: str, sql: str = "SELECT 1;\n") -> None:
    (project / "migrations" / name).write_text(sql)


def _save_history(project: Path, value: dict) -> Path:
    path = project / "migrations" / "_history.json"
    FileHistoryStore(path).save(value)
    return path


def _run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    code = exc.value.code
    assert isinstance(code, int)
    return code


def test_global_json_flag_reaches_status_handler(project: Path, capsys) -> None:
    _flat(project, "001_legacy.sql")

    assert _run(["--json", "status", "--env", "dev"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "env": "dev",
        "applied": [],
        "pending": ["001_legacy.sql"],
        "drift": [],
        "last_synced_at": None,
        "last_synced_by": None,
    }


def test_status_reuses_existing_migracli_history(project: Path, capsys) -> None:
    _flat(project, "001_legacy.sql")
    (project / "migrations" / "migration_history.json").write_text(
        json.dumps(
            {
                "dev": {
                    "001_legacy.sql": {
                        "applied_at": "2026-01-01T00:00:00+00:00",
                        "applied_by": "tester",
                        "method": "executed",
                    }
                }
            }
        )
    )

    assert _run(["status", "--env", "dev", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "env": "dev",
        "applied": ["001_legacy.sql"],
        "pending": [],
        "drift": [],
        "last_synced_at": None,
        "last_synced_by": None,
    }


def test_run_accepts_documented_yes_flag() -> None:
    args = build_parser().parse_args(["run", "all", "--env", "prod", "--confirm-prod", "--yes", "--json"])
    assert args.yes is True


def test_run_uses_source_history_without_recording(project: Path, monkeypatch, capsys) -> None:
    _flat(project, "001_existing.sql")
    _flat(project, "002_pending.sql")
    path = _save_history(
        project,
        {
            "_meta": {"schema_version": 2},
            "dev": {
                "001_existing.sql": {
                    "applied_at": "2026-01-01T00:00:00+00:00",
                    "applied_by": "tester",
                    "method": "executed",
                    "duration_seconds": 0.1,
                    "events": [],
                }
            },
        },
    )
    before = path.read_text()
    monkeypatch.setenv("DATABASE_URL_LOCAL", "postgresql://user:pass@invalid/unused")
    executed: list[str] = []

    def fake_execute(url, migration, dry_run=False):
        executed.append(migration.name)
        return migrator.MigrationResult(status="applied", duration=0.01)

    monkeypatch.setattr(migrator, "execute", fake_execute)

    rc = _run(
        [
            "run",
            "all",
            "--env",
            "local",
            "--history-env",
            "dev",
            "--no-record-history",
            "--json",
        ]
    )

    assert rc == 0
    assert executed == ["002_pending.sql"]
    assert path.read_text() == before
    payload = json.loads(capsys.readouterr().out)
    assert payload["history_env"] == "dev"
    assert payload["record_history"] is False


def test_targeted_dry_run_can_rerun_applied_migration(project: Path, monkeypatch, capsys) -> None:
    _flat(project, "001_existing.sql")
    _save_history(
        project,
        {
            "_meta": {"schema_version": 2},
            "dev": {
                "001_existing.sql": {
                    "applied_at": "2026-01-01T00:00:00+00:00",
                    "applied_by": "tester",
                    "method": "executed",
                    "duration_seconds": 0.1,
                    "events": [],
                }
            },
        },
    )
    monkeypatch.setenv("DATABASE_URL_DEV", "postgresql://user:pass@invalid/unused")
    calls: list[tuple[str, bool]] = []

    def fake_execute(url, migration, dry_run=False):
        calls.append((migration.name, dry_run))
        return migrator.MigrationResult(status="applied", duration=0.01)

    monkeypatch.setattr(migrator, "execute", fake_execute)

    assert _run(["run", "001_existing.sql", "--env", "dev", "--dry-run", "--json"]) == 0
    assert calls == [("001_existing.sql", True)]
    assert json.loads(capsys.readouterr().out)["summary"]["applied"] == 1


def test_run_no_match_is_failure_with_json(project: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("DATABASE_URL_DEV", "postgresql://user:pass@invalid/unused")

    assert _run(["run", "999_missing.sql", "--env", "dev", "--json"]) == 1

    captured = capsys.readouterr()
    assert "No migration matches" in captured.err
    assert json.loads(captured.out)["status"] == "failed"


def test_history_env_requires_no_record_history(capsys) -> None:
    assert _run(["run", "all", "--env", "local", "--history-env", "dev"]) == 2
    assert "requires --no-record-history" in capsys.readouterr().err


# --- new --oneoff ---


def test_new_oneoff_creates_flat_file(project: Path, capsys) -> None:
    _flat(project, "001_existing.sql")
    rc = _run(["new", "fix_creator_names", "--oneoff", "--json"])
    assert rc == 0
    path = project / "migrations" / "002_fix_creator_names.sql"
    assert path.is_file()
    content = path.read_text()
    assert "-- one-off: 002_fix_creator_names" in content
    assert "-- Rollback:" in content
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "oneoff"
    assert payload["migration"] == "002_fix_creator_names.sql"


# --- exec ---


def test_exec_applies_and_records_who_when(project: Path, monkeypatch, capsys) -> None:
    script = project / "fix.sql"
    script.write_text("UPDATE posts SET paid = true;\n")
    monkeypatch.setenv("DATABASE_URL_LOCAL", "postgresql://user:pass@invalid/unused")

    def fake_execute(url, migration, dry_run=False):
        return migrator.MigrationResult(status="applied", duration=0.01)

    monkeypatch.setattr(migrator, "execute", fake_execute)

    rc = _run(["exec", str(script), "--env", "local", "--json"])
    assert rc == 0
    hist = json.loads((project / "migrations" / "_history.json").read_text())
    entry = hist["local"]["fix.sql"]
    assert entry["applied_at"]
    assert entry["applied_by"]
    assert entry["method"] == "exec"
    assert entry["content_hash"]
    assert entry["events"][0]["by"]
    assert entry["events"][0]["at"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "success"


def test_exec_blocks_rerun_of_same_file(project: Path, monkeypatch, capsys) -> None:
    script = project / "fix.sql"
    script.write_text("UPDATE posts SET paid = true;\n")
    monkeypatch.setenv("DATABASE_URL_LOCAL", "postgresql://user:pass@invalid/unused")
    calls: list[str] = []

    def fake_execute(url, migration, dry_run=False):
        calls.append(migration.name)
        return migrator.MigrationResult(status="applied", duration=0.01)

    monkeypatch.setattr(migrator, "execute", fake_execute)

    assert _run(["exec", str(script), "--env", "local", "--json"]) == 0
    capsys.readouterr()
    assert _run(["exec", str(script), "--env", "local", "--json"]) == 1
    assert calls == ["fix.sql"]  # second run blocked before executing
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["applied_by"]


def test_exec_blocks_identical_content_under_different_name(project: Path, monkeypatch, capsys) -> None:
    first = project / "fix.sql"
    first.write_text("SELECT 42;\n")
    renamed = project / "fix_v2.sql"
    renamed.write_text("SELECT 42;\n")
    monkeypatch.setenv("DATABASE_URL_LOCAL", "postgresql://user:pass@invalid/unused")
    calls: list[str] = []

    def fake_execute(url, migration, dry_run=False):
        calls.append(migration.name)
        return migrator.MigrationResult(status="applied", duration=0.01)

    monkeypatch.setattr(migrator, "execute", fake_execute)

    assert _run(["exec", str(first), "--env", "local", "--json"]) == 0
    capsys.readouterr()
    assert _run(["exec", str(renamed), "--env", "local", "--json"]) == 1
    assert calls == ["fix.sql"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert "identical content" in payload["error"]


def test_exec_allow_rerun_overrides_block(project: Path, monkeypatch, capsys) -> None:
    script = project / "fix.sql"
    script.write_text("SELECT 1;\n")
    monkeypatch.setenv("DATABASE_URL_LOCAL", "postgresql://user:pass@invalid/unused")
    calls: list[str] = []

    def fake_execute(url, migration, dry_run=False):
        calls.append(migration.name)
        return migrator.MigrationResult(status="applied", duration=0.01)

    monkeypatch.setattr(migrator, "execute", fake_execute)

    assert _run(["exec", str(script), "--env", "local", "--json"]) == 0
    assert _run(["exec", str(script), "--env", "local", "--allow-rerun", "--json"]) == 0
    assert calls == ["fix.sql", "fix.sql"]


def test_exec_dry_run_records_nothing(project: Path, monkeypatch, capsys) -> None:
    script = project / "fix.sql"
    script.write_text("SELECT 1;\n")
    monkeypatch.setenv("DATABASE_URL_LOCAL", "postgresql://user:pass@invalid/unused")

    def fake_execute(url, migration, dry_run=False):
        return migrator.MigrationResult(status="applied", duration=0.01)

    monkeypatch.setattr(migrator, "execute", fake_execute)

    assert _run(["exec", str(script), "--env", "local", "--dry-run", "--json"]) == 0
    assert not (project / "migrations" / "_history.json").exists()


def test_exec_missing_file_fails(project: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("DATABASE_URL_LOCAL", "postgresql://user:pass@invalid/unused")
    assert _run(["exec", str(project / "nope.sql"), "--env", "local", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"


def test_status_json_includes_sync_fields(project: Path, capsys) -> None:
    _flat(project, "001_existing.sql")
    _save_history(
        project,
        {
            "_meta": {"schema_version": 2, "last_synced_at": "2026-01-02T00:00:00+00:00", "last_synced_by": "tester"},
            "local": {},
        },
    )
    assert _run(["status", "--env", "local", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["last_synced_at"] == "2026-01-02T00:00:00+00:00"
    assert payload["last_synced_by"] == "tester"
