"""Project configuration: discover and load `chozo.toml`.

A project is any directory tree containing a `chozo.toml` (resolved by walking
up from the cwd, like git discovers `.git`). The config pins the migrations
directory and the environment -> env-var mapping. With no config, chozo falls
back to the default `local`/`dev`/`prod` -> `DATABASE_URL_*` convention.

`read_project(root)` reads config for a known root (used by the registry);
`load_config(start)` walks up from a directory to find the root first.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from chozo.constants import CONFIG_FILENAME, DEFAULT_ENVS

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover - py3.12+ always has tomllib
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass
class ProjectConfig:
    root: Path
    name: str
    migrations_dir: Path
    envs: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ENVS))
    # Registry layer fields (None/False => plain local project, no registry).
    slug: str | None = None
    registered: bool = False
    history_override: Path | None = None

    @property
    def history_path(self) -> Path:
        if self.history_override is not None:
            return self.history_override
        return self.migrations_dir / "_history.json"


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) looking for a chozo.toml or migrations dir."""
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / CONFIG_FILENAME).is_file():
            return candidate
        if (candidate / "migrations").is_dir():
            return candidate
    return None


def read_project(root: Path) -> ProjectConfig:
    """Read config for a known root. Falls back to defaults when no chozo.toml."""
    root = root.expanduser().resolve()
    config_file = root / CONFIG_FILENAME
    envs = dict(DEFAULT_ENVS)
    if config_file.is_file():
        data = tomllib.loads(config_file.read_text())
        project = data.get("project", {})
        name = project.get("name", root.name)
        migrations_dir = (root / project.get("migrations_dir", "migrations")).resolve()
        for env_name, env_cfg in data.get("envs", {}).items():
            if "url_var" in env_cfg:
                envs[env_name] = env_cfg["url_var"]
    else:
        name = root.name
        migrations_dir = (root / "migrations").resolve()
    return ProjectConfig(root=root, name=name, migrations_dir=migrations_dir, envs=envs)


def load_config(start: Path | None = None) -> ProjectConfig:
    """Resolve the project config for a directory (walks up to find the root)."""
    root = find_project_root(start) or Path.cwd().resolve()
    return read_project(root)


def require_env_url(envs: dict[str, str], env: str) -> str:
    """Resolve the connection string for `env` from its configured env var."""
    import os

    var = envs.get(env)
    if var is None:
        sys.stderr.write(f"error: unknown environment '{env}'. known: {', '.join(sorted(envs)) or '(none)'}\n")
        sys.exit(1)
    url = os.environ.get(var)
    if not url:
        sys.stderr.write(f"error: environment variable {var} is not set.\n")
        sys.exit(1)
    return url
