"""History: v2 schema, record, pending, remove, merge."""

import json
from pathlib import Path

from chozo import history
from chozo.history import FileHistoryStore


def _store(tmp_path: Path) -> tuple[FileHistoryStore, Path]:
    path = tmp_path / "history.json"
    return FileHistoryStore(path), path


def test_load_creates_empty_meta(tmp_path: Path) -> None:
    store, path = _store(tmp_path)
    hist = history.load(store)
    assert hist["_meta"]["schema_version"] == 2
    assert not path.exists()  # load does not write


def test_record_marks_applied_and_appends_event(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    hist = history.load(store)
    history.record(store, hist, "dev", "001_add_users", method="executed", duration_seconds=0.12)
    entry = hist["dev"]["001_add_users"]
    assert entry["applied_at"] is not None
    assert entry["method"] == "executed"
    assert len(entry["events"]) == 1
    assert entry["events"][0]["success"] is True


def test_failed_attempt_records_event_without_applied(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    hist = history.load(store)
    history.record(store, hist, "dev", "001_add_users", method="executed", success=False, error="boom")
    entry = hist["dev"]["001_add_users"]
    assert entry["applied_at"] is None
    assert entry["events"][0]["success"] is False
    assert entry["events"][0]["error"] == "boom"


def test_get_pending(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    hist = history.load(store)
    history.record(store, hist, "dev", "001_add_users", method="executed")
    pending = history.get_pending("dev", hist, ["001_add_users", "002_add_email"])
    assert pending == ["002_add_email"]


def test_remove(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    hist = history.load(store)
    history.record(store, hist, "dev", "001_add_users", method="executed")
    assert history.remove(store, hist, "dev", "001_add_users") is True
    assert "001_add_users" not in hist["dev"]
    assert history.remove(store, hist, "dev", "001_add_users") is False


def test_rollback_keeps_event_trail_and_marks_pending(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    hist = history.load(store)
    history.record(store, hist, "dev", "001_add_users", method="executed")

    history.record_rollback(store, hist, "dev", "001_add_users", duration_seconds=0.2)

    entry = hist["dev"]["001_add_users"]
    assert entry["applied_at"] is None
    assert [event["action"] for event in entry["events"]] == ["executed", "rolled_back"]
    assert history.get_pending("dev", hist, ["001_add_users"]) == ["001_add_users"]


def test_schema_v1_migrated_to_v2(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    path.write_text(json.dumps({"dev": {"001_x": {"applied_at": "2025-01-01T00:00:00", "method": "executed"}}}))
    store = FileHistoryStore(path)
    hist = history.load(store)
    assert hist["_meta"]["schema_version"] == 2
    assert hist["dev"]["001_x"]["events"][0]["action"] == "executed"


def test_merge_histories_unions_and_dedups(tmp_path: Path) -> None:
    local = {
        "_meta": {"schema_version": 2},
        "dev": {
            "001_a": {
                "applied_at": "2025-01-01T00:00:00",
                "applied_by": None,
                "method": "executed",
                "duration_seconds": None,
                "events": [{"at": "2025-01-01T00:00:00", "action": "executed"}],
            }
        },
    }
    remote = {
        "_meta": {"schema_version": 2},
        "dev": {
            "001_a": {
                "applied_at": "2025-01-01T00:00:00",
                "applied_by": None,
                "method": "executed",
                "duration_seconds": None,
                "events": [{"at": "2025-01-01T00:00:00", "action": "executed"}],
            },
            "002_b": {
                "applied_at": "2025-01-02T00:00:00",
                "applied_by": None,
                "method": "executed",
                "duration_seconds": None,
                "events": [{"at": "2025-01-02T00:00:00", "action": "executed"}],
            },
        },
    }
    merged = history.merge_histories(local, remote)
    assert set(merged["dev"]) == {"001_a", "002_b"}
    assert len(merged["dev"]["001_a"]["events"]) == 1  # deduped by (at, action)


def test_merge_uses_latest_rollback_state() -> None:
    applied = {
        "applied_at": "2026-01-01T00:00:00+00:00",
        "applied_by": "tester",
        "method": "executed",
        "duration_seconds": 0.1,
        "events": [{"at": "2026-01-01T00:00:00+00:00", "action": "executed"}],
    }
    rolled_back = {
        "applied_at": None,
        "applied_by": None,
        "method": None,
        "duration_seconds": None,
        "events": [
            {"at": "2026-01-01T00:00:00+00:00", "action": "executed"},
            {"at": "2026-01-02T00:00:00+00:00", "action": "rolled_back"},
        ],
    }

    merged = history.merge_histories(
        {"_meta": {"schema_version": 2}, "dev": {"001_a": rolled_back}},
        {"_meta": {"schema_version": 2}, "dev": {"001_a": applied}},
    )

    assert merged["dev"]["001_a"]["applied_at"] is None
    assert len(merged["dev"]["001_a"]["events"]) == 2
