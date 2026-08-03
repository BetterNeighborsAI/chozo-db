"""Migration history.

A history file is a JSON document with a `_meta` block and per-env entries.
Each entry keeps a top-level applied pointer plus the full `events[]` trail (every
attempt, success or failure, with actor + duration + error). The store is a
simple file path today; a `~/.chozo` registry can swap this out later without
touching the engine.
"""

from __future__ import annotations

import getpass
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from chozo.constants import HISTORY_SCHEMA_VERSION


class HistoryStore(Protocol):
    def load(self) -> dict: ...
    def save(self, history: dict) -> None: ...


class FileHistoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"_meta": {"schema_version": HISTORY_SCHEMA_VERSION}}

    def save(self, history: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        history.setdefault("_meta", {})["schema_version"] = HISTORY_SCHEMA_VERSION
        self.path.write_text(json.dumps(history, indent=2) + "\n")


def migrate_schema(history: dict) -> dict:
    """Normalize older flat histories into the v2 _meta + events[] shape."""
    if history.get("_meta", {}).get("schema_version") == HISTORY_SCHEMA_VERSION:
        return history
    out: dict = {"_meta": {"schema_version": HISTORY_SCHEMA_VERSION}}
    for key, value in history.items():
        if key == "_meta":
            continue
        out[key] = {}
        for filename, entry in value.items():
            events = entry.get("events") if isinstance(entry, dict) else None
            if not events and isinstance(entry, dict):
                events = [
                    {
                        "action": entry.get("method", "unknown"),
                        "at": entry.get("applied_at"),
                        "by": entry.get("applied_by"),
                        "duration_seconds": None,
                        "success": True,
                    }
                ]
            out[key][filename] = {
                "applied_at": entry.get("applied_at") if isinstance(entry, dict) else None,
                "applied_by": entry.get("applied_by") if isinstance(entry, dict) else None,
                "method": entry.get("method") if isinstance(entry, dict) else None,
                "duration_seconds": entry.get("duration_seconds") if isinstance(entry, dict) else None,
                "events": events or [],
            }
    return out


def load(store: HistoryStore) -> dict:
    return migrate_schema(store.load())


def get_pending(env: str, history: dict, names: list[str]) -> list[str]:
    applied = history.get(env, {})
    return [n for n in names if n not in applied or not applied[n].get("applied_at")]


def get_applied_names(env: str, history: dict) -> list[str]:
    env_data = history.get(env, {})
    return [name for name, entry in env_data.items() if isinstance(entry, dict) and entry.get("applied_at")]


def record(
    store: HistoryStore,
    history: dict,
    env: str,
    name: str,
    method: str,
    duration_seconds: float | None = None,
    success: bool = True,
    error: str | None = None,
    content_hash: str | None = None,
) -> None:
    if env not in history:
        history[env] = {}
    now = datetime.now(UTC).isoformat()
    user = getpass.getuser()
    event = {
        "action": method,
        "at": now,
        "by": user,
        "duration_seconds": duration_seconds,
        "success": success,
    }
    if error:
        event["error"] = error

    entry = history[env].get(name)
    if entry is None:
        entry = {
            "applied_at": None,
            "applied_by": None,
            "method": None,
            "duration_seconds": None,
            "content_hash": None,
            "events": [],
        }
        history[env][name] = entry
    entry["events"].append(event)
    if success:
        entry["applied_at"] = now
        entry["applied_by"] = user
        entry["method"] = method
        entry["duration_seconds"] = duration_seconds
        if content_hash is not None:
            entry["content_hash"] = content_hash
    store.save(history)


def record_rollback(
    store: HistoryStore,
    history: dict,
    env: str,
    name: str,
    duration_seconds: float | None = None,
) -> None:
    """Record a successful DOWN while retaining the migration's audit trail."""
    if env not in history:
        history[env] = {}
    entry = history[env].setdefault(
        name,
        {
            "applied_at": None,
            "applied_by": None,
            "method": None,
            "duration_seconds": None,
            "events": [],
        },
    )
    entry["events"].append(
        {
            "action": "rolled_back",
            "at": datetime.now(UTC).isoformat(),
            "by": getpass.getuser(),
            "duration_seconds": duration_seconds,
            "success": True,
        }
    )
    entry["applied_at"] = None
    entry["applied_by"] = None
    entry["method"] = None
    entry["duration_seconds"] = None
    store.save(history)


def remove(store: HistoryStore, history: dict, env: str, name: str) -> bool:
    env_data = history.get(env, {})
    if name not in env_data:
        return False
    del env_data[name]
    store.save(history)
    return True


def merge_histories(local: dict, remote: dict) -> dict:
    """Union histories, dedup events, and take state from the latest event."""
    merged: dict = {"_meta": local.get("_meta", {}).copy()}
    all_envs = {k for k in [*local.keys(), *remote.keys()] if k != "_meta"}
    for env in all_envs:
        l_env = local.get(env, {})
        r_env = remote.get(env, {})
        merged_env: dict = {}
        for filename in sorted(set(l_env) | set(r_env)):
            l_entry = l_env.get(filename)
            r_entry = r_env.get(filename)
            if l_entry and not r_entry:
                merged_env[filename] = l_entry
            elif r_entry and not l_entry:
                merged_env[filename] = r_entry
            else:
                assert l_entry is not None and r_entry is not None
                seen: set[tuple[str, str]] = set()
                all_events = []
                for ev in [*l_entry.get("events", []), *r_entry.get("events", [])]:
                    key = (ev.get("at") or "", ev.get("action") or "")
                    if key not in seen:
                        seen.add(key)
                        all_events.append(ev)
                all_events.sort(key=lambda e: e.get("at") or "")
                l_at = max([l_entry.get("applied_at") or "", *(ev.get("at") or "" for ev in l_entry.get("events", []))])
                r_at = max([r_entry.get("applied_at") or "", *(ev.get("at") or "" for ev in r_entry.get("events", []))])
                winner = r_entry if r_at > l_at else l_entry
                merged_env[filename] = {
                    "applied_at": winner.get("applied_at"),
                    "applied_by": winner.get("applied_by"),
                    "method": winner.get("method"),
                    "duration_seconds": winner.get("duration_seconds"),
                    "events": all_events,
                }
        merged[env] = merged_env
    return merged
