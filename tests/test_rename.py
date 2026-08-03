"""Rename detection: renamed one-off content can't silently re-run."""

import json
from pathlib import Path

import pytest

from chozo import migrator
from chozo.cli import main
from chozo.output import Output


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "app"
    (root / "migrations").mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.setenv("CHOZO_HOME", str(tmp_path / "chozo-home"))
    monkeypatch.delenv("GCS_MIGRATIONS_BUCKET", raising=False)
    monkeypatch.setenv("DATABASE_URL_LOCAL", "postgresql://user:pass@invalid/unused")
    return root


def _run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    code = exc.value.code
    assert isinstance(code, int)
    return code


def _fake_execute(monkeypatch) -> list[str]:
    calls: list[str] = []

    def fake(url, migration, dry_run=False):
        calls.append(migration.name)
        return migrator.MigrationResult(status="applied", duration=0.01)

    monkeypatch.setattr(migrator, "execute", fake)
    return calls


def _flat(root: Path, name: str, sql: str) -> None:
    (root / "migrations" / name).write_text(sql)


def _rename(root: Path, old: str, new: str) -> None:
    (root / "migrations" / old).rename(root / "migrations" / new)


def test_renamed_oneoff_blocked_in_agent_mode(project: Path, monkeypatch, capsys) -> None:
    _flat(project, "001_seed_data.sql", "INSERT INTO t VALUES (1);\n")
    _fake_execute(monkeypatch)
    assert _run(["mark", "--env", "local", "--yes"]) == 0  # records content_hash
    capsys.readouterr()  # drain the setup command's output
    _rename(project, "001_seed_data.sql", "001_upload_data.sql")

    rc = _run(["run", "all", "--env", "local", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["skipped"] == 1
    entry = payload["migrations"][0]
    assert entry["result"] == "blocked"
    assert "already applied as '001_seed_data.sql'" in entry["error"]
    assert "--allow-rerun" in entry["error"]


def test_allow_rerun_permits_renamed_content(project: Path, monkeypatch, capsys) -> None:
    _flat(project, "001_seed_data.sql", "INSERT INTO t VALUES (1);\n")
    calls = _fake_execute(monkeypatch)
    _run(["mark", "--env", "local", "--yes"])
    capsys.readouterr()  # drain the setup command's output
    _rename(project, "001_seed_data.sql", "001_upload_data.sql")

    rc = _run(["run", "all", "--env", "local", "--allow-rerun", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["applied"] == 1
    assert calls == ["001_upload_data.sql"]


def test_renamed_oneoff_interactive_decline(project: Path, monkeypatch) -> None:
    _flat(project, "001_seed_data.sql", "INSERT INTO t VALUES (1);\n")
    calls = _fake_execute(monkeypatch)
    _run(["mark", "--env", "local", "--yes"])
    _rename(project, "001_seed_data.sql", "001_upload_data.sql")

    # Interactive `up`: confirm "apply pending?" then decline the rename rerun.
    answers = iter([True, False])
    monkeypatch.setattr(Output, "confirm", lambda self, msg, default=False: next(answers))

    rc = _run(["up", "--env", "local"])

    assert rc == 0
    assert calls == []  # declined -> not executed


def test_renamed_oneoff_interactive_accept(project: Path, monkeypatch) -> None:
    _flat(project, "001_seed_data.sql", "INSERT INTO t VALUES (1);\n")
    calls = _fake_execute(monkeypatch)
    _run(["mark", "--env", "local", "--yes"])
    _rename(project, "001_seed_data.sql", "001_upload_data.sql")

    answers = iter([True, True])  # apply pending? yes. rerun renamed? yes.
    monkeypatch.setattr(Output, "confirm", lambda self, msg, default=False: next(answers))

    rc = _run(["up", "--env", "local"])

    assert rc == 0
    assert calls == ["001_upload_data.sql"]


def test_mark_records_content_hash(project: Path, monkeypatch) -> None:

    _flat(project, "001_seed_data.sql", "INSERT INTO t VALUES (1);\n")
    _run(["mark", "--env", "local", "--yes"])
    hist = json.loads((project / "migrations" / "_history.json").read_text())
    entry = hist["local"]["001_seed_data.sql"]
    assert entry["content_hash"] is not None
    assert len(entry["content_hash"]) == 64  # sha256 hex


def test_status_reports_drift(project: Path, monkeypatch, capsys) -> None:
    _flat(project, "001_seed_data.sql", "INSERT INTO t VALUES (1);\n")
    _run(["mark", "--env", "local", "--yes"])
    capsys.readouterr()  # drain the setup command's output
    # Edit the applied migration's content (drift).
    _flat(project, "001_seed_data.sql", "INSERT INTO t VALUES (2);\n")

    rc = _run(["status", "--env", "local", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["drift"] == ["001_seed_data.sql"]
