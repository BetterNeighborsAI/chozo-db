"""Content-hash integrity: hashing, rename detection, drift."""

from pathlib import Path

from chozo import integrity
from chozo.discovery import Migration


def _migration(root: Path, name: str, up: str, down: str | None = None) -> Migration:
    directory = root / name
    directory.mkdir(parents=True)
    up_path = directory / "up.sql"
    up_path.write_text(up)
    down_path = None
    if down is not None:
        down_path = directory / "down.sql"
        down_path.write_text(down)
    return Migration(name=name, number=int(name.split("_", 1)[0]), dir=directory, up=up_path, down=down_path)


def test_content_hash_rename_invariant(tmp_path: Path) -> None:
    a = _migration(tmp_path / "x", "001_a", "SELECT 1;")
    b = _migration(tmp_path / "y", "001_b", "SELECT 1;")
    assert integrity.content_hash(a) == integrity.content_hash(b)


def test_content_hash_changes_with_content(tmp_path: Path) -> None:
    mig = _migration(tmp_path, "001_a", "SELECT 1;")
    before = integrity.content_hash(mig)
    mig.up.write_text("SELECT 2;")
    assert integrity.content_hash(mig) != before


def test_content_hash_includes_down(tmp_path: Path) -> None:
    with_down = _migration(tmp_path / "x", "001_a", "SELECT 1;", down="SELECT 0;")
    without_down = _migration(tmp_path / "y", "001_b", "SELECT 1;")
    assert integrity.content_hash(with_down) != integrity.content_hash(without_down)


def test_find_duplicate_same_hash_different_name() -> None:
    hist = {"dev": {"001_old": {"applied_at": "2026-01-01T00:00:00", "content_hash": "abc", "events": []}}}
    assert integrity.find_duplicate("abc", hist, "dev", exclude_name="001_new") == {
        "name": "001_old",
        "applied_at": "2026-01-01T00:00:00",
    }


def test_find_duplicate_ignores_same_name() -> None:
    hist = {"dev": {"001_a": {"applied_at": "t", "content_hash": "abc", "events": []}}}
    assert integrity.find_duplicate("abc", hist, "dev", exclude_name="001_a") is None


def test_find_duplicate_ignores_pending() -> None:
    hist = {"dev": {"001_old": {"applied_at": None, "content_hash": "abc", "events": []}}}
    assert integrity.find_duplicate("abc", hist, "dev", exclude_name="001_new") is None


def test_find_duplicate_ignores_missing_hash() -> None:
    hist = {"dev": {"001_old": {"applied_at": "t", "events": []}}}
    assert integrity.find_duplicate("abc", hist, "dev", exclude_name="001_new") is None


def test_find_duplicate_does_not_cross_envs() -> None:
    hist = {"prod": {"001_old": {"applied_at": "t", "content_hash": "abc", "events": []}}}
    assert integrity.find_duplicate("abc", hist, "dev", exclude_name="001_new") is None


def test_find_drift_changed(tmp_path: Path) -> None:
    mig = _migration(tmp_path, "001_a", "SELECT 1;")
    hist = {"dev": {"001_a": {"applied_at": "t", "content_hash": "different", "events": []}}}
    assert integrity.find_drift(mig, integrity.content_hash(mig), hist, "dev") is True


def test_find_drift_unchanged(tmp_path: Path) -> None:
    mig = _migration(tmp_path, "001_a", "SELECT 1;")
    digest = integrity.content_hash(mig)
    hist = {"dev": {"001_a": {"applied_at": "t", "content_hash": digest, "events": []}}}
    assert integrity.find_drift(mig, digest, hist, "dev") is False


def test_find_drift_not_applied(tmp_path: Path) -> None:
    mig = _migration(tmp_path, "001_a", "SELECT 1;")
    assert integrity.find_drift(mig, integrity.content_hash(mig), {"dev": {}}, "dev") is False


def test_find_drift_no_stored_hash(tmp_path: Path) -> None:
    mig = _migration(tmp_path, "001_a", "SELECT 1;")
    hist = {"dev": {"001_a": {"applied_at": "t", "events": []}}}
    assert integrity.find_drift(mig, integrity.content_hash(mig), hist, "dev") is False
