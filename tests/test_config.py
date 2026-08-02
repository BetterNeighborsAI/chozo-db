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
