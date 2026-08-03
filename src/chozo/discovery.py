"""Migration discovery.

A native migration is a directory `NNN_<name>/` containing `up.sql` and
(optionally) `down.sql`. A migracli-compatible flat `NNN_<name>.sql` file is a
one-way migration. The numeric prefix is the ordering key. A missing `down.sql`
marks the migration as one-way: `rollback` refuses it but `up`/`status` work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from chozo.constants import DOWN_FILENAME, LEGACY_MIGRATION_NAME_RE, MIGRATION_NAME_RE, UP_FILENAME

_NAME_RE = re.compile(MIGRATION_NAME_RE)
_LEGACY_NAME_RE = re.compile(LEGACY_MIGRATION_NAME_RE)


@dataclass(frozen=True)
class Migration:
    name: str  # directory or flat filename, e.g. "003_add_email_index"
    number: int  # numeric prefix
    dir: Path
    up: Path
    down: Path | None

    @property
    def has_down(self) -> bool:
        return self.down is not None and self.down.is_file()


def discover(migrations_dir: Path) -> list[Migration]:
    """Return native and migracli-compatible migrations in execution order."""
    if not migrations_dir.is_dir():
        return []
    found: list[Migration] = []
    for child in migrations_dir.iterdir():
        if child.is_dir() and _NAME_RE.fullmatch(child.name):
            up = child / UP_FILENAME
            if not up.is_file():
                continue
            number = int(child.name.split("_", 1)[0])
            down = child / DOWN_FILENAME
            found.append(Migration(name=child.name, number=number, dir=child, up=up, down=down))
        elif child.is_file() and _LEGACY_NAME_RE.fullmatch(child.name):
            number = int(child.name.split("_", 1)[0])
            found.append(Migration(name=child.name, number=number, dir=migrations_dir, up=child, down=None))
    found.sort(key=lambda m: (m.number, m.name))
    return found


def next_number(migrations_dir: Path) -> int:
    existing = discover(migrations_dir)
    return (existing[-1].number + 1) if existing else 1
