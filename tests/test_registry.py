"""Registry: register / resolve / isolation / list / use / unregister."""

import json
from pathlib import Path

import pytest

from chozo import registry
from chozo.history import FileHistoryStore


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "chozo-home"
    monkeypatch.setenv("CHOZO_HOME", str(h))
    return h


def _make_project(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / "migrations").mkdir()
    return root


def test_register_creates_slug_and_project_json(home: Path, tmp_path: Path) -> None:
    root = _make_project(tmp_path, "my-app")
    result = registry.register(root)
    assert result["slug"] == "my-app"
    assert (home / "projects" / "my-app" / "project.json").exists()
    reg = json.loads((home / "registry.json").read_text())
    assert reg["projects"]["my-app"]["root"] == str(root.resolve())


def test_register_is_idempotent_by_root(home: Path, tmp_path: Path) -> None:
    root = _make_project(tmp_path, "my-app")
    first = registry.register(root)
    second = registry.register(root)
    assert first["slug"] == second["slug"] == "my-app"
    assert len(registry.load_registry()["projects"]) == 1


def test_register_unique_slugs_on_name_collision(home: Path, tmp_path: Path) -> None:
    a = _make_project(tmp_path / "a", "app")
    b = _make_project(tmp_path / "b", "app")
    sa = registry.register(a)["slug"]
    sb = registry.register(b)["slug"]
    assert sa == "app"
    assert sb == "app-2"


def test_register_migrates_local_history_into_registry(home: Path, tmp_path: Path) -> None:
    root = _make_project(tmp_path, "my-app")
    local_history = root / "migrations" / "_history.json"
    local_history.write_text(
        json.dumps({"dev": {"001_x": {"applied_at": "2025-01-01T00:00:00", "method": "executed"}}})
    )
    result = registry.register(root)
    assert result["history_migrated"] is True
    assert not local_history.exists()
    assert (root / "migrations" / "_history.json.archived").exists()
    merged = FileHistoryStore(registry.project_history_path("my-app")).load()
    assert "001_x" in merged["dev"]


def test_resolve_by_cwd_match(home: Path, tmp_path: Path) -> None:
    root = _make_project(tmp_path, "my-app")
    registry.register(root)
    cfg = registry.resolve(start=root / "migrations")
    assert cfg.registered is True
    assert cfg.slug == "my-app"
    assert cfg.history_path == registry.project_history_path("my-app")


def test_resolve_explicit_slug_wins_over_cwd(home: Path, tmp_path: Path) -> None:
    app_a = _make_project(tmp_path, "app-a")
    app_b = _make_project(tmp_path, "app-b")
    registry.register(app_a)
    registry.register(app_b)
    # From inside app-b, explicitly ask for app-a -> must resolve app-a (isolation).
    cfg = registry.resolve(explicit_slug="app-a", start=app_b)
    assert cfg.slug == "app-a"
    assert cfg.root == app_a.resolve()


def test_resolve_longest_cwd_match_wins(home: Path, tmp_path: Path) -> None:
    outer = _make_project(tmp_path, "outer")
    inner = _make_project(tmp_path / "outer" / "sub", "inner")
    registry.register(outer)
    registry.register(inner)
    cfg = registry.resolve(start=inner / "migrations")
    assert cfg.slug == "inner"


def test_resolve_falls_back_to_current_marker(home: Path, tmp_path: Path) -> None:
    root = _make_project(tmp_path, "my-app")
    registry.register(root)
    registry.set_current("my-app")
    elsewhere = tmp_path / "nowhere"
    elsewhere.mkdir()
    cfg = registry.resolve(start=elsewhere)
    assert cfg.slug == "my-app"


def test_resolve_falls_back_to_local_project(home: Path, tmp_path: Path) -> None:
    root = _make_project(tmp_path, "local-app")
    cfg = registry.resolve(start=root)
    assert cfg.registered is False
    assert cfg.slug is None
    assert cfg.history_path == root / "migrations" / "_history.json"


def test_resolve_unknown_slug_raises(home: Path, tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        registry.resolve(explicit_slug="nope", start=tmp_path)


def test_resolve_missing_registered_root_raises(home: Path, tmp_path: Path) -> None:
    root = _make_project(tmp_path, "doomed")
    registry.register(root)
    import shutil

    shutil.rmtree(root)
    with pytest.raises(FileNotFoundError):
        registry.resolve(explicit_slug="doomed", start=tmp_path)


def test_unregister_removes_entry_and_dir_but_not_source(home: Path, tmp_path: Path) -> None:
    root = _make_project(tmp_path, "my-app")
    registry.register(root)
    assert registry.unregister("my-app") is True
    assert "my-app" not in registry.load_registry()["projects"]
    assert not (home / "projects" / "my-app").exists()
    assert root.is_dir()  # source untouched
    assert registry.unregister("my-app") is False


def test_unregister_clears_current_marker(home: Path, tmp_path: Path) -> None:
    root = _make_project(tmp_path, "my-app")
    registry.register(root)
    registry.set_current("my-app")
    registry.unregister("my-app")
    assert registry.get_current() is None


def test_use_and_list(home: Path, tmp_path: Path) -> None:
    root = _make_project(tmp_path, "my-app")
    registry.register(root)
    assert registry.set_current("my-app") is True
    assert registry.set_current("nope") is False
    rows = registry.list_projects()
    assert len(rows) == 1
    assert rows[0]["slug"] == "my-app"
    assert rows[0]["current"] is True
    assert rows[0]["exists"] is True


def test_history_written_to_registry_path_not_repo(home: Path, tmp_path: Path) -> None:
    root = _make_project(tmp_path, "my-app")
    registry.register(root)
    cfg = registry.resolve(start=root)
    store = FileHistoryStore(cfg.history_path)
    from chozo import history

    hist = history.load(store)
    history.record(store, hist, "dev", "001_a", method="executed")
    assert (registry.project_history_path("my-app")).exists()
    assert not (root / "migrations" / "_history.json").exists()
