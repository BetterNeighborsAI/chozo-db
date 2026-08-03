"""Discovery: native migration dirs and migracli-compatible flat SQL."""

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


def test_discovers_flat_migracli_file_as_one_way(tmp_path: Path) -> None:
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    legacy = mdir / "007_legacy_change.sql"
    legacy.write_text("SELECT 1;\n")

    migs = discovery.discover(mdir)

    assert [m.name for m in migs] == ["007_legacy_change.sql"]
    assert migs[0].up == legacy
    assert migs[0].has_down is False


def test_sorts_native_and_flat_migrations_together(tmp_path: Path) -> None:
    mdir = tmp_path / "migrations"
    _make(mdir, "003_native")
    mdir.mkdir(exist_ok=True)
    (mdir / "002_legacy.sql").write_text("SELECT 1;\n")

    assert [m.name for m in discovery.discover(mdir)] == ["002_legacy.sql", "003_native"]


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


def test_next_number_includes_flat_migrations(tmp_path: Path) -> None:
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "014_legacy.sql").write_text("SELECT 1;\n")
    assert discovery.next_number(mdir) == 15


def test_next_number_starts_at_one(tmp_path: Path) -> None:
    assert discovery.next_number(tmp_path / "migrations") == 1


def test_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert discovery.discover(tmp_path / "nope") == []
