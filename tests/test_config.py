"""Config: chozo.toml discovery + default env fallback."""

from pathlib import Path

from chozo import config


def test_default_config_when_no_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = config.load_config()
    assert cfg.root == tmp_path.resolve()
    assert cfg.envs == {"local": "DATABASE_URL_LOCAL", "dev": "DATABASE_URL_DEV", "prod": "DATABASE_URL"}
    assert cfg.migrations_dir == (tmp_path / "migrations").resolve()


def test_toml_overrides_envs_and_dir(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "chozo.toml").write_text(
        """[project]
name = "my-app"
migrations_dir = "db/migrations"

[envs.staging]
url_var = "DATABASE_URL_STAGING"
"""
    )
    monkeypatch.chdir(tmp_path)
    cfg = config.load_config()
    assert cfg.name == "my-app"
    assert cfg.migrations_dir == (tmp_path / "db/migrations").resolve()
    assert cfg.envs["staging"] == "DATABASE_URL_STAGING"
    # defaults still present
    assert cfg.envs["prod"] == "DATABASE_URL"


def test_require_env_url_errors_on_unknown(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = config.load_config()
    envs = cfg.envs
    import pytest

    with pytest.raises(SystemExit):
        config.require_env_url(envs, "nope")


def test_require_env_url_reads_env_var(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL_DEV", "postgresql://u:p@localhost/db")
    cfg = config.load_config()
    assert config.require_env_url(cfg.envs, "dev") == "postgresql://u:p@localhost/db"


def test_existing_migracli_history_file_is_reused(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    legacy = migrations / "migration_history.json"
    legacy.write_text("{}\n")

    assert config.read_project(tmp_path).history_path == legacy


def test_native_history_takes_precedence_when_both_exist(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "migration_history.json").write_text("{}\n")
    native = migrations / "_history.json"
    native.write_text("{}\n")

    assert config.read_project(tmp_path).history_path == native


def test_zero_config_discovers_migracli_sql_layout(tmp_path: Path) -> None:
    migrations = tmp_path / "sql" / "migrations"
    migrations.mkdir(parents=True)
    nested = tmp_path / "scripts" / "migrations"
    nested.mkdir(parents=True)

    cfg = config.load_config(nested)

    assert cfg.root == tmp_path.resolve()
    assert cfg.migrations_dir == migrations.resolve()


def test_toml_parses_sync_section(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "chozo.toml").write_text(
        """[project]
name = "my-app"

[sync]
bucket = "chozo-migrations"
"""
    )
    monkeypatch.chdir(tmp_path)
    cfg = config.load_config()
    assert cfg.sync.bucket == "chozo-migrations"
    assert cfg.sync.path is None
    assert cfg.sync_slug == "my-app"


def test_toml_sync_explicit_path(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "chozo.toml").write_text(
        """[project]
name = "my-app"

[sync]
bucket = "chozo-migrations"
path = "custom/state.json"
"""
    )
    monkeypatch.chdir(tmp_path)
    cfg = config.load_config()
    assert cfg.sync.path == "custom/state.json"


def test_sync_slug_prefers_registry_slug(tmp_path: Path) -> None:
    cfg = config.ProjectConfig(
        root=tmp_path, name="My App", migrations_dir=tmp_path / "migrations", slug="registered-slug"
    )
    assert cfg.sync_slug == "registered-slug"


def test_sync_slug_slugifies_name(tmp_path: Path) -> None:
    cfg = config.ProjectConfig(root=tmp_path, name="My  App!", migrations_dir=tmp_path / "migrations")
    assert cfg.sync_slug == "my-app"
