"""Optional remote synchronization for migration history.

The execution engine only depends on the ``HistoryStore`` protocol. This module
wraps a local store with a remote backend, preserving local writes when the
network or optional cloud dependency is unavailable. GCS environment variables
remain compatible with migracli.
"""

from __future__ import annotations

import copy
import getpass
import importlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from chozo import history
from chozo.history import HistoryStore

GCS_BUCKET_ENV = "GCS_MIGRATIONS_BUCKET"
GCS_PATH_ENV = "GCS_MIGRATIONS_PATH"
GCS_DEFAULT_PATH = "migrations/migration_history.json"


class SyncError(RuntimeError):
    """Remote history synchronization failed."""


class HistoryRemote(Protocol):
    def pull(self) -> dict | None: ...
    def push(self, value: dict) -> None: ...


class GCSHistoryRemote:
    def __init__(self, bucket: str, path: str = GCS_DEFAULT_PATH) -> None:
        self.bucket = bucket
        self.path = path

    def _blob(self):
        try:
            storage = importlib.import_module("google.cloud.storage")
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional extra
            raise SyncError("GCS sync requires `chozo-db[gcs]` (google-cloud-storage).") from exc
        try:
            client = storage.Client()
            return client, client.bucket(self.bucket).blob(self.path)
        except Exception as exc:  # pragma: no cover - cloud client behavior
            raise SyncError(f"could not initialize GCS history backend: {exc}") from exc

    def pull(self) -> dict | None:
        client, blob = self._blob()
        try:
            if not blob.exists(client=client):
                return None
            value = json.loads(blob.download_as_text())
        except Exception as exc:  # pragma: no cover - cloud client behavior
            raise SyncError(f"could not pull gs://{self.bucket}/{self.path}: {exc}") from exc
        if not isinstance(value, dict):
            raise SyncError(f"remote history gs://{self.bucket}/{self.path} is not a JSON object.")
        return value

    def push(self, value: dict) -> None:
        _, blob = self._blob()
        try:
            blob.upload_from_string(json.dumps(value, indent=2) + "\n", content_type="application/json")
        except Exception as exc:  # pragma: no cover - cloud client behavior
            raise SyncError(f"could not push gs://{self.bucket}/{self.path}: {exc}") from exc


def gcs_remote_from_env() -> GCSHistoryRemote | None:
    bucket = os.environ.get(GCS_BUCKET_ENV)
    if not bucket:
        return None
    return GCSHistoryRemote(bucket, os.environ.get(GCS_PATH_ENV, GCS_DEFAULT_PATH))


def _stamp(value: dict) -> None:
    meta = value.setdefault("_meta", {})
    meta["last_synced_at"] = datetime.now(UTC).isoformat()
    meta["last_synced_by"] = getpass.getuser()


@dataclass(frozen=True)
class SyncResult:
    history: dict
    remote_found: bool


def synchronize(local: HistoryStore, remote: HistoryRemote) -> SyncResult:
    """Merge local/remote state and persist the same stamped result to both."""
    local_value = history.load(local)
    remote_value = remote.pull()
    merged = (
        history.merge_histories(local_value, history.migrate_schema(remote_value))
        if remote_value is not None
        else local_value
    )
    _stamp(merged)
    # Keep the merged result locally even if the following remote push fails.
    local.save(merged)
    remote.push(merged)
    return SyncResult(history=merged, remote_found=remote_value is not None)


class SyncedHistoryStore:
    """History store that pulls on load and pushes after successful local saves."""

    def __init__(self, local: HistoryStore, remote: HistoryRemote, warn=None) -> None:
        self.local = local
        self.remote = remote
        self.warn = warn or (lambda _message: None)

    def load(self) -> dict:
        local_value = history.migrate_schema(self.local.load())
        try:
            remote_value = self.remote.pull()
        except SyncError as exc:
            self.warn(f"History sync pull failed: {exc}")
            return local_value
        if remote_value is None:
            return local_value
        merged = history.merge_histories(local_value, history.migrate_schema(remote_value))
        if merged != local_value:
            self.local.save(merged)
        return merged

    def save(self, history: dict) -> None:
        # The local history is authoritative during an outage: never trade a
        # successful migration record for remote availability.
        self.local.save(history)
        synced = copy.deepcopy(history)
        _stamp(synced)
        try:
            self.remote.push(synced)
        except SyncError as exc:
            self.warn(f"History sync push failed: {exc}")
            return
        history.clear()
        history.update(synced)
        self.local.save(history)
