"""Account-only configuration and init contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from inspire.cli.commands.init.discover import _sanitize_account_config
from inspire.cli.main import main as cli_main
from inspire.config import (
    CONFIG_OPTIONS,
    SOURCE_ACCOUNT,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_ENV_FILE,
    Config,
    ConfigError,
    get_option_by_toml,
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith(("INSPIRE_", "INSP_")):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return fake_home


def _write_account(home: Path, name: str, content: str) -> Path:
    path = home / ".inspire" / "accounts" / name / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    (home / ".inspire" / "current").write_text(f"{name}\n", encoding="utf-8")
    return path


def test_every_config_option_is_account_scoped() -> None:
    assert CONFIG_OPTIONS
    assert all(option.scope == "global" for option in CONFIG_OPTIONS)
    fields = Config.__dataclass_fields__
    assert all(option.field_name in fields for option in CONFIG_OPTIONS)


def test_toml_keys_map_to_loader_fields() -> None:
    assert get_option_by_toml("auth.username").field_name == "username"
    assert get_option_by_toml("api.base_url").field_name == "base_url"
    assert get_option_by_toml("job.shm_size").field_name == "shm_size"
    assert get_option_by_toml("notebook.post_start").field_name == "notebook_post_start"
    assert get_option_by_toml("missing.key") is None


def test_toml_helpers_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[auth]\nusername = "alice"\n[api]\nbase_url = "https://x"\n')
    data = Config._load_toml(path)
    assert data["auth"]["username"] == "alice"
    assert Config._flatten_toml(data) == {
        "auth.username": "alice",
        "api.base_url": "https://x",
    }
    assert Config._toml_key_to_field("auth.username") == "username"


def test_defaults_do_not_depend_on_repository(
    home: Path, clean_env: None, tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    del home
    monkeypatch.chdir(tmp_path)
    cfg, sources = Config.from_files_and_env(require_credentials=False)
    assert cfg.username == ""
    assert cfg.base_url == "https://qz.sii.edu.cn"
    assert cfg.shm_size is None
    assert sources["username"] == SOURCE_DEFAULT


def test_account_config_loads_identity_and_workload_behavior(
    home: Path,
    clean_env: None,
) -> None:
    _write_account(
        home,
        "alice",
        '[auth]\nusername = "alice-login"\npassword = "pw"\n'
        '[api]\nbase_url = "https://qz.sii.edu.cn"\n'
        '[job]\nshm_size = 64\nauto_fault_tolerance = true\n'
        'fault_tolerance_max_retry = 6\nenable_notification = true\n'
        '[notebook]\npost_start = "bash setup.sh"\n'
        '[remote_env]\nWANDB_MODE = "offline"\n',
    )

    cfg, sources = Config.from_files_and_env()

    assert cfg.username == "alice-login"
    assert cfg.password == "pw"
    assert cfg.shm_size == 64
    assert cfg.job_auto_fault_tolerance is True
    assert cfg.job_fault_tolerance_max_retry == 6
    assert cfg.job_enable_notification is True
    assert cfg.notebook_post_start == "bash setup.sh"
    assert cfg.remote_env == {"WANDB_MODE": "offline"}
    assert sources["shm_size"] == SOURCE_ACCOUNT


def test_environment_overrides_runtime_but_not_account_identity(
    home: Path,
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_account(
        home,
        "alice",
        '[auth]\nusername = "toml-user"\npassword = "pw"\n'
        '[job]\nshm_size = 32\n',
    )
    monkeypatch.setenv("INSPIRE_USERNAME", "env-user")
    monkeypatch.setenv("INSPIRE_SHM_SIZE", "96")

    cfg, sources = Config.from_files_and_env()

    assert cfg.username == "toml-user"
    assert cfg.shm_size == 96
    assert sources["username"] == SOURCE_ACCOUNT
    assert sources["shm_size"] == SOURCE_ENV


def test_repository_inspire_directory_is_never_read(
    home: Path,
    clean_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_account(home, "alice", '[auth]\nusername = "alice"\npassword = "pw"\n')
    repo = tmp_path / "repo"
    project_config = repo / ".inspire" / "config.toml"
    project_config.parent.mkdir(parents=True)
    project_config.write_text(
        '[auth]\nusername = "poison"\n[context]\nproject = "Old"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)

    cfg, _ = Config.from_files_and_env()

    assert cfg.username == "alice"


def test_legacy_account_catalogs_and_profiles_are_ignored(
    home: Path,
    clean_env: None,
) -> None:
    _write_account(
        home,
        "alice",
        '[auth]\nusername = "alice"\npassword = "pw"\n'
        '[context]\nproject = "Old"\n'
        '[path_aliases]\nme = "/inspire/old"\n'
        '[profiles.job.old]\nworkspace = "Old"\n'
        '[projects]\nshort = "Old"\n',
    )

    cfg, _ = Config.from_files_and_env()

    assert cfg.username == "alice"
    assert not hasattr(cfg, "path_aliases")
    assert not hasattr(cfg, "profiles")
    assert not hasattr(cfg, "context_project")


def test_missing_active_account_fails_when_credentials_are_required(
    home: Path,
    clean_env: None,
) -> None:
    del home
    with pytest.raises(ConfigError, match="account add"):
        Config.from_files_and_env()


def test_writable_config_path_targets_active_account(home: Path, clean_env: None) -> None:
    path = _write_account(home, "alice", "")
    assert Config.writable_config_path() == path


def test_init_template_writes_only_account_config(
    home: Path,
    clean_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _write_account(home, "alice", "")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli_main, ["init", "--template", "--force"])

    assert result.exit_code == 0, result.output
    content = account.read_text(encoding="utf-8")
    assert "[auth]" in content
    assert "[job]" in content
    assert "[notebook]" in content
    assert not (tmp_path / ".inspire").exists()


def test_init_rejects_removed_project_scope(
    home: Path,
    clean_env: None,
) -> None:
    _write_account(home, "alice", "")
    result = CliRunner().invoke(cli_main, ["init", "--scope", "project"])
    assert result.exit_code != 0
    assert "No such option '--scope'" in result.output


def test_init_smart_mode_writes_workload_env_to_account(
    home: Path,
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _write_account(home, "alice", "")
    monkeypatch.setenv("INSPIRE_SHM_SIZE", "72")

    result = CliRunner().invoke(cli_main, ["init", "--no-discover", "--force"])

    assert result.exit_code == 0, result.output
    assert "[job]" in account.read_text(encoding="utf-8")
    assert "shm_size = 72" in account.read_text(encoding="utf-8")


def test_init_smart_mode_preserves_existing_account_identity(
    home: Path,
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _write_account(
        home,
        "alice",
        '[auth]\nusername = "alice-login"\npassword = "secret"\n',
    )
    monkeypatch.setenv("INSPIRE_SHM_SIZE", "72")

    result = CliRunner().invoke(cli_main, ["init", "--no-discover", "--force"])

    assert result.exit_code == 0, result.output
    content = account.read_text(encoding="utf-8")
    assert 'username = "alice-login"' in content
    assert 'password = "secret"' in content
    assert "shm_size = 72" in content


def test_init_json_output_is_one_document(
    home: Path,
    clean_env: None,
) -> None:
    _write_account(home, "alice", "")
    result = CliRunner().invoke(
        cli_main,
        ["--json", "init", "--template", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["status"] == "updated"


def test_sanitizer_drops_all_retired_repository_state() -> None:
    cleaned = _sanitize_account_config(
        {
            "auth": {"username": "alice"},
            "api": {"base_url": "https://qz.sii.edu.cn", "docker_registry": "old"},
            "context": {"project": "Old"},
            "path_aliases": {"me": "/old"},
            "profiles": {"job": {"old": {"workspace": "Old"}}},
            "projects": {"old": "Old"},
            "project_catalog": {"old": {}},
            "compute_groups": [{"name": "old"}],
            "paths": {"target": "/old"},
            "future": {"enabled": True},
        }
    )
    assert cleaned == {
        "auth": {"username": "alice"},
        "api": {"base_url": "https://qz.sii.edu.cn"},
        "future": {"enabled": True},
    }


def test_explicit_env_file_is_loaded_and_labeled(
    home: Path,
    clean_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_account(home, "alice", '[auth]\nusername = "alice"\npassword = "pw"\n')
    env_file = tmp_path / "run.env"
    env_file.write_text("INSPIRE_SHM_SIZE=88\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    from inspire.cli import env_bootstrap

    env_bootstrap.bootstrap_env_file(env_file=env_file)
    cfg, sources = Config.from_files_and_env()

    assert cfg.shm_size == 88
    assert sources["shm_size"] == SOURCE_ENV_FILE
    env_bootstrap.reset_loaded_env_file_state()
