"""Destructive-operation detection.

Instead of a hardcoded file blocklist, chozo parses each migration's UP SQL and
refuses to auto-apply anything that looks destructive. Dry runs are always
allowed because they `ROLLBACK` and cannot commit. There is deliberately no
runtime escape hatch — to run a blocked migration, apply it by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (label, compiled regex). Matched against comment-stripped, uppercased SQL.
_DESTRUCTIVE: list[tuple[str, re.Pattern[str]]] = [
    ("DROP TABLE", re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE)),
    ("DROP DATABASE", re.compile(r"\bDROP\s+DATABASE\b", re.IGNORECASE)),
    ("DROP SCHEMA", re.compile(r"\bDROP\s+SCHEMA\b", re.IGNORECASE)),
    ("TRUNCATE", re.compile(r"\bTRUNCATE\b", re.IGNORECASE)),
    # Unqualified DELETE (no WHERE) wipes the whole table. Match up to the
    # statement terminator so a WHERE clause lands inside the match and clears it.
    ("DELETE FROM without WHERE", re.compile(r"\bDELETE\s+FROM\b[^;]*;?", re.IGNORECASE)),
]

_LINE_COMMENT = re.compile(r"--.*?$", re.MULTILINE)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


@dataclass(frozen=True)
class Finding:
    label: str
    snippet: str


def _strip_comments(sql: str) -> str:
    no_block = _BLOCK_COMMENT.sub(" ", sql)
    return _LINE_COMMENT.sub("", no_block)


def analyze(sql: str) -> list[Finding]:
    """Return destructive findings in `sql`. Empty list means safe to auto-apply."""
    cleaned = _strip_comments(sql)
    findings: list[Finding] = []
    for label, pattern in _DESTRUCTIVE:
        for match in pattern.finditer(cleaned):
            snippet = re.sub(r"\s+", " ", match.group(0)).strip()[:80]
            if label == "DELETE FROM without WHERE" and re.search(r"\bWHERE\b", match.group(0), re.IGNORECASE):
                continue
            findings.append(Finding(label=label, snippet=snippet))
    return findings


def is_destructive(sql: str) -> bool:
    return bool(analyze(sql))
