"""The `~/.chozo` multi-project registry.

A machine-local index of projects, with a per-project history store and a
"current" convenience default. Isolation is structural: every command resolves
exactly one project and reads only that project's history, env mapping, and
migrations dir. Nothing crosses projects.

Layout::

    ~/.chozo/
      registry.json                  # {schema_version, current, projects: {slug: entry}}
      projects/
        <slug>/
          project.json               # {schema_version, slug, name, root, created_at, last_used_at}
          history.json               # the v2 history document for this project

Resolution order for the active project (first hit wins)::

    1. --project <slug>              (explicit, what agents use)
    2. cwd matches a registered root (longest match; like git discovers .git)
    3. the "current" marker          (set by `chozo use <slug>`)
    4. fall back to a local, unregistered project (chozo.toml / defaults)

Set CHOZO_HOME to relocate the registry (used by tests).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from chozo import config, history
from chozo.history import FileHistoryStore

REGISTRY_SCHEMA_VERSION = 1
REGISTRY_FILENAME = "registry.json"
CHOZO_HOME_ENV = "CHOZO_HOME"


# --- paths ---


def chozo_home() -> Path:
    override = os.environ.get(CHOZO_HOME_ENV)
    return Path(override).expanduser().resolve() if override else Path.home() / ".chozo"


def registry_file() -> Path:
    return chozo_home() / REGISTRY_FILENAME


def projects_dir() -> Path:
    return chozo_home() / "projects"


def project_dir(slug: str) -> Path:
    return projects_dir() / slug


def project_json_path(slug: str) -> Path:
    return project_dir(slug) / "project.json"


def project_history_path(slug: str) -> Path:
    return project_dir(slug) / "history.json"


# --- registry document ---


def _now() -> str:
    return datetime.now(UTC).isoformat()


def load_registry() -> dict:
    path = registry_file()
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
    data.setdefault("schema_version", REGISTRY_SCHEMA_VERSION)
    data.setdefault("current", None)
    data.setdefault("projects", {})
    return data


def save_registry(reg: dict) -> None:
    reg["schema_version"] = REGISTRY_SCHEMA_VERSION
    path = registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reg, indent=2) + "\n")


# --- slugs ---


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug or "project"


def unique_slug(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


# --- registration ---


def register(root: Path, name: str | None = None, slug: str | None = None) -> dict:
    """Register a project root. Idempotent by root: re-registering updates, never duplicates.

    Returns a dict {slug, name, root, history_migrated}.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"project root does not exist: {root}")

    cfg = config.read_project(root)
    reg = load_registry()

    # Idempotent: same root -> update name, keep the existing slug.
    for existing_slug, entry in reg["projects"].items():
        if Path(entry["root"]).expanduser().resolve() == root:
            if name:
                entry["name"] = name
            entry["last_used_at"] = _now()
            save_registry(reg)
            _write_project_json(existing_slug, entry)
            return {"slug": existing_slug, "name": entry["name"], "root": str(root), "history_migrated": False}

    base = slugify(name or cfg.name or root.name)
    chosen = unique_slug(slug or base, set(reg["projects"]))
    now = _now()
    entry = {"name": name or cfg.name, "root": str(root), "created_at": now, "last_used_at": now}
    reg["projects"][chosen] = entry

    project_dir(chosen).mkdir(parents=True, exist_ok=True)
    _write_project_json(chosen, entry)

    # Move any existing local history into the registry store (merge, then archive).
    history_migrated = _migrate_local_history(cfg, chosen)

    save_registry(reg)
    return {"slug": chosen, "name": entry["name"], "root": str(root), "history_migrated": history_migrated}


def _write_project_json(slug: str, entry: dict) -> None:
    payload = {"schema_version": REGISTRY_SCHEMA_VERSION, "slug": slug, **entry}
    path = project_json_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _migrate_local_history(cfg: config.ProjectConfig, slug: str) -> bool:
    local_path = cfg.history_path
    if not local_path.exists():
        return False
    local = json.loads(local_path.read_text())
    registry_store = FileHistoryStore(project_history_path(slug))
    merged = history.merge_histories(history.load(registry_store), history.migrate_schema(local))
    registry_store.save(merged)
    local_path.rename(local_path.with_name(f"{local_path.name}.archived"))
    return True


def unregister(slug: str) -> bool:
    """Remove a project from the registry and delete its registry dir. Never touches source."""
    reg = load_registry()
    if slug not in reg["projects"]:
        return False
    del reg["projects"][slug]
    if reg.get("current") == slug:
        reg["current"] = None
    save_registry(reg)
    shutil.rmtree(project_dir(slug), ignore_errors=True)
    return True


# --- current marker ---


def set_current(slug: str) -> bool:
    reg = load_registry()
    if slug not in reg["projects"]:
        return False
    reg["current"] = slug
    save_registry(reg)
    return True


def get_current() -> str | None:
    return load_registry().get("current")


# --- listing ---


def list_projects() -> list[dict]:
    reg = load_registry()
    current = reg.get("current")
    rows = []
    for slug, entry in sorted(reg["projects"].items()):
        rows.append(
            {
                "slug": slug,
                "name": entry.get("name"),
                "root": entry.get("root"),
                "current": slug == current,
                "exists": Path(entry.get("root", "")).expanduser().is_dir(),
                "last_used_at": entry.get("last_used_at"),
            }
        )
    return rows


# --- resolution ---


def resolve(explicit_slug: str | None = None, start: Path | None = None) -> config.ProjectConfig:
    """Resolve the active project per the order documented at the top of this module."""
    reg = load_registry()
    projects = reg["projects"]
    cwd = (start or Path.cwd()).resolve()

    if explicit_slug is not None:
        entry = projects.get(explicit_slug)
        if entry is None:
            raise KeyError(f"unknown project slug '{explicit_slug}'. Known: {', '.join(sorted(projects)) or '(none)'}.")
        return _from_entry(explicit_slug, entry)

    best = _match_cwd(cwd, projects)
    if best is not None:
        return _from_entry(best, projects[best])

    current = reg.get("current")
    if current and current in projects:
        return _from_entry(current, projects[current])

    return config.load_config(cwd)


def _match_cwd(cwd: Path, projects: dict) -> str | None:
    best: str | None = None
    best_len = -1
    for slug, entry in projects.items():
        root = Path(entry["root"]).expanduser().resolve()
        if root == cwd or root in cwd.parents:
            depth = len(str(root))
            if depth > best_len:
                best_len = depth
                best = slug
    return best


def _from_entry(slug: str, entry: dict) -> config.ProjectConfig:
    root = Path(entry["root"]).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(
            f"registered project root is missing: {root} (slug '{slug}'). Re-register or `chozo unregister {slug}`."
        )
    cfg = config.read_project(root)
    cfg.name = entry.get("name") or cfg.name  # registered name is canonical
    cfg.slug = slug
    cfg.registered = True
    cfg.history_override = project_history_path(slug)
    _touch(slug, entry)
    return cfg


def _touch(slug: str, entry: dict) -> None:
    reg = load_registry()
    if slug in reg["projects"]:
        reg["projects"][slug]["last_used_at"] = _now()
        save_registry(reg)
