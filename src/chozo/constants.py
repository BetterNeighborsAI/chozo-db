"""Shared constants for chozo."""

from __future__ import annotations

APP_NAME = "chozo"
APP_TAGLINE = "guardian of knowledge"

# Active history schema. v2 adds `_meta` and a per-migration `events[]` trail.
HISTORY_SCHEMA_VERSION = 2
HISTORY_FILENAME = "_history.json"

# Per-project config file, discovered by walking up from the working directory.
CONFIG_FILENAME = "chozo.toml"

# A migration is a directory named `NNN_<snake>` containing `up.sql` + `down.sql`.
MIGRATION_NAME_RE = r"^\d{3}_[a-z0-9_]+$"
UP_FILENAME = "up.sql"
DOWN_FILENAME = "down.sql"

# Default environment -> env-var mapping when no chozo.toml overrides it.
DEFAULT_ENVS: dict[str, str] = {
    "local": "DATABASE_URL_LOCAL",
    "dev": "DATABASE_URL_DEV",
    "prod": "DATABASE_URL",
}

# Exit codes used by every non-interactive mode.
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NOTHING_TO_DO = 2
