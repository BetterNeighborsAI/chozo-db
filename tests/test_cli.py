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
    assert payload == {"env": "dev", "applied": [], "pending": ["001_legacy.sql"], "drift": []}


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
    assert payload == {"env": "dev", "applied": ["001_legacy.sql"], "pending": [], "drift": []}


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
