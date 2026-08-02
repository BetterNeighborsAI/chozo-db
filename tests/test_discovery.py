"""Discovery: migration dirs with up.sql + down.sql."""

from pathlib import Path

from chozo import discovery


def _make(root: Path, name: str, *, up: bool = True, down: bool = True) -> None:
    d = root / name
    d.mkdir(parents=True)
    if up:
        (d / "up.sql").write_text("-- up\n")
    if down:
        (d / "down.sql").write_text("-- down\n")


def test_discovers_and_sorts_by_number(tmp_path: Path) -> None:
    mdir = tmp_path / "migrations"
    _make(mdir, "003_add_index")
    _make(mdir, "001_add_users")
    _make(mdir, "002_add_email")
    migs = discovery.discover(mdir)
    assert [m.name for m in migs] == ["001_add_users", "002_add_email", "003_add_index"]


def test_ignores_non_migration_dirs_and_files(tmp_path: Path) -> None:
    mdir = tmp_path / "migrations"
    _make(mdir, "001_ok")
    (mdir / "notes.txt").write_text("ignore me")
    (mdir / "999").mkdir()  # no underscore
    migs = discovery.discover(mdir)
    assert [m.name for m in migs] == ["001_ok"]


def test_requires_up_sql(tmp_path: Path) -> None:
    mdir = tmp_path / "migrations"
    _make(mdir, "001_only_down", up=False, down=True)
    assert discovery.discover(mdir) == []


def test_down_optional_marks_one_way(tmp_path: Path) -> None:
    mdir = tmp_path / "migrations"
    _make(mdir, "001_no_down", up=True, down=False)
    migs = discovery.discover(mdir)
    assert len(migs) == 1
    assert migs[0].has_down is False


def test_next_number_continues_sequence(tmp_path: Path) -> None:
    mdir = tmp_path / "migrations"
    _make(mdir, "001_a")
    _make(mdir, "005_b")
    assert discovery.next_number(mdir) == 6


def test_next_number_starts_at_one(tmp_path: Path) -> None:
    assert discovery.next_number(tmp_path / "migrations") == 1


def test_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert discovery.discover(tmp_path / "nope") == []
