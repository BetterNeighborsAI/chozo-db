"""Migration discovery.

A migration is a directory `NNN_<name>/` containing `up.sql` and (optionally)
`down.sql`. The numeric prefix is the ordering key. A missing `down.sql` marks
the migration as one-way: `rollback` will refuse it but `up`/`status` are fine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from chozo.constants import DOWN_FILENAME, MIGRATION_NAME_RE, UP_FILENAME

_NAME_RE = re.compile(MIGRATION_NAME_RE)


@dataclass(frozen=True)
class Migration:
    name: str  # directory name, e.g. "003_add_email_index"
    number: int  # numeric prefix
    dir: Path
    up: Path
    down: Path | None

    @property
    def has_down(self) -> bool:
        return self.down is not None and self.down.is_file()


def discover(migrations_dir: Path) -> list[Migration]:
    """Return all migrations in `migrations_dir`, sorted by numeric prefix then name."""
    if not migrations_dir.is_dir():
        return []
    found: list[Migration] = []
    for child in migrations_dir.iterdir():
        if not child.is_dir() or not _NAME_RE.match(child.name):
            continue
        up = child / UP_FILENAME
        if not up.is_file():
            continue
        number = int(child.name.split("_", 1)[0])
        down = child / DOWN_FILENAME
        found.append(Migration(name=child.name, number=number, dir=child, up=up, down=down))
    found.sort(key=lambda m: (m.number, m.name))
    return found


def next_number(migrations_dir: Path) -> int:
    existing = discover(migrations_dir)
    return (existing[-1].number + 1) if existing else 1
