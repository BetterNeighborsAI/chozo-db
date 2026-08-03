"""Remote history synchronization without a real cloud backend."""

from pathlib import Path

from chozo import history, sync
from chozo.history import FileHistoryStore


class FakeRemote:
    def __init__(self, value: dict | None = None) -> None:
        self.value = value
        self.pushes: list[dict] = []

    def pull(self) -> dict | None:
        return self.value

    def push(self, value: dict) -> None:
        self.value = value
        self.pushes.append(value)


class FailingRemote(FakeRemote):
    def pull(self) -> dict | None:
        raise sync.SyncError("offline")

    def push(self, value: dict) -> None:
        raise sync.SyncError("offline")


def _entry(at: str) -> dict:
    return {
        "applied_at": at,
        "applied_by": "tester",
        "method": "executed",
        "duration_seconds": 0.1,
        "events": [{"action": "executed", "at": at, "by": "tester", "success": True}],
    }


def test_synced_store_merges_remote_on_load(tmp_path: Path) -> None:
    local = FileHistoryStore(tmp_path / "history.json")
    local.save({"_meta": {"schema_version": 2}, "dev": {"001_local": _entry("2026-01-01")}})
    remote = FakeRemote({"_meta": {"schema_version": 2}, "dev": {"002_remote": _entry("2026-01-02")}})

    value = history.load(sync.SyncedHistoryStore(local, remote))

    assert set(value["dev"]) == {"001_local", "002_remote"}
    assert set(local.load()["dev"]) == {"001_local", "002_remote"}


def test_synced_store_pushes_and_stamps_after_local_save(tmp_path: Path) -> None:
    local = FileHistoryStore(tmp_path / "history.json")
    remote = FakeRemote()
    store = sync.SyncedHistoryStore(local, remote)
    value = history.load(store)

    history.record(store, value, "dev", "001_change", method="executed")

    assert remote.pushes
    assert remote.pushes[-1]["dev"]["001_change"]["applied_at"]
    assert remote.pushes[-1]["_meta"]["last_synced_at"]
    assert local.load()["_meta"]["last_synced_at"]


def test_synced_store_keeps_local_writes_when_remote_is_offline(tmp_path: Path) -> None:
    local = FileHistoryStore(tmp_path / "history.json")
    warnings: list[str] = []
    store = sync.SyncedHistoryStore(local, FailingRemote(), warn=warnings.append)
    value = history.load(store)

    history.record(store, value, "dev", "001_change", method="executed")

    assert local.load()["dev"]["001_change"]["applied_at"]
    assert len(warnings) == 2
    assert all("offline" in warning for warning in warnings)


def test_explicit_synchronize_unions_and_pushes(tmp_path: Path) -> None:
    local = FileHistoryStore(tmp_path / "history.json")
    local.save({"_meta": {"schema_version": 2}, "dev": {"001_local": _entry("2026-01-01")}})
    remote = FakeRemote({"_meta": {"schema_version": 2}, "prod": {"002_remote": _entry("2026-01-02")}})

    result = sync.synchronize(local, remote)

    assert result.remote_found is True
    assert "001_local" in result.history["dev"]
    assert "002_remote" in result.history["prod"]
    assert remote.pushes[-1] == local.load()


def test_gcs_remote_from_env(monkeypatch) -> None:
    monkeypatch.setenv(sync.GCS_BUCKET_ENV, "bucket")
    monkeypatch.setenv(sync.GCS_PATH_ENV, "custom/history.json")
    remote = sync.gcs_remote_from_env()
    assert remote is not None
    assert remote.bucket == "bucket"
    assert remote.path == "custom/history.json"
