"""Content-hash integrity for migrations.

File *names* are mutable; file *content* is the real identity. Chozo hashes each
migration's SQL (UP, plus DOWN when present) and stores the digest in history.
That powers two protections:

- **Rename detection** — a migration whose content was already applied under a
  different name is caught on apply, so a renamed one-off (e.g. a data upload)
  can't silently re-run. The user is asked "already ran this as '<old>', are you
  sure?" (interactive) or must pass ``--allow-rerun`` (agent mode).
- **Drift detection** — ``status`` flags any applied migration whose on-disk
  content no longer matches the hash recorded when it was applied.
"""

from __future__ import annotations

import hashlib

from chozo.discovery import Migration


def content_hash(migration: Migration) -> str:
    """SHA-256 of the migration's UP SQL plus DOWN SQL (when present)."""
    digest = hashlib.sha256()
    digest.update(migration.up.read_bytes())
    if migration.down is not None and migration.down.is_file():
        digest.update(b"\x00")  # separator so up/down can't collide across files
        digest.update(migration.down.read_bytes())
    return digest.hexdigest()


def find_duplicate(digest: str, history: dict, env: str, exclude_name: str) -> dict | None:
    """Return the applied entry with the same content under a *different* name.

    Returns {"name", "applied_at"} or None. Only entries that recorded a
    content hash can match (older entries without one are skipped).
    """
    for name, entry in history.get(env, {}).items():
        if name == exclude_name or not isinstance(entry, dict):
            continue
        if entry.get("content_hash") == digest and entry.get("applied_at"):
            return {"name": name, "applied_at": entry.get("applied_at")}
    return None


def find_drift(migration: Migration, digest: str, history: dict, env: str) -> bool:
    """True if this migration was applied under the same name but its content changed."""
    entry = history.get(env, {}).get(migration.name)
    if not isinstance(entry, dict) or not entry.get("applied_at"):
        return False
    stored = entry.get("content_hash")
    return stored is not None and stored != digest
