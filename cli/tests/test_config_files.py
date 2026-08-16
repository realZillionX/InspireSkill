"""Tests for TOML config file loading and layered configuration."""

import json
import os
import re
from pathlib import Path
from typing import Generator

import pytest
from click.testing import CliRunner

from inspire.config import (
    Config,
    ConfigError,
    SOURCE_DEFAULT,
    SOURCE_ACCOUNT,
    SOURCE_PROJECT,
    SOURCE_ENV,
    SOURCE_ENV_FILE,
    PROJECT_CONFIG_DIR,
    CONFIG_FILENAME,
)
from inspire.config import (
    CONFIG_OPTIONS,
    get_categories,
    get_options_by_category,
    get_options_by_scope,
    get_option_by_env,
    get_option_by_toml,
)
from inspire.cli.commands.init import init
from inspire.cli.commands.init.env_detect import _detect_env_vars, _generate_toml_content
from inspire.cli.commands.account import account as account_command
from inspire.cli.commands.config import config as config_command
from inspire.cli.main import main as cli_main

# ===========================================================================
# Config Schema tests
# ===========================================================================


class TestConfigSchema:
    """Tests for config schema module."""

    def test_config_options_not_empty(self) -> None:
        """Test that CONFIG_OPTIONS has entries."""
        assert len(CONFIG_OPTIONS) > 0

    def test_all_options_have_required_fields(self) -> None:
        """Test that all options have required fields."""
        for opt in CONFIG_OPTIONS:
            assert opt.env_var, f"Option missing env_var: {opt}"
            assert opt.toml_key, f"Option missing toml_key: {opt}"
            assert opt.field_name, f"Option missing field_name: {opt}"
            assert opt.description, f"Option missing description: {opt}"
            assert opt.category, f"Option missing category: {opt}"

    def test_get_option_by_env(self) -> None:
        """Test getting option by env var."""
        opt = get_option_by_env("INSPIRE_USERNAME")
        assert opt is not None
        assert opt.toml_key == "auth.username"

    def test_get_option_by_toml(self) -> None:
        """Test getting option by TOML key."""
        opt = get_option_by_toml("auth.username")
        assert opt is not None
        assert opt.env_var == "INSPIRE_USERNAME"
        proxy_opt = get_option_by_toml("proxy.requests_http")
        assert proxy_opt is not None
        assert proxy_opt.env_var == "INSPIRE_REQUESTS_HTTP_PROXY"

    def test_get_option_not_found(self) -> None:
        """Test getting non-existent option."""
        assert get_option_by_env("NONEXISTENT_VAR") is None
        assert get_option_by_toml("nonexistent.key") is None

    def test_get_categories(self) -> None:
        """Test getting all categories."""
        categories = get_categories()
        assert len(categories) > 0
        assert "Authentication" in categories
        assert "API" in categories
        assert "Proxy" in categories
        assert "Workspaces" not in categories

    def test_get_options_by_category(self) -> None:
        """Test getting options by category."""
        auth_opts = get_options_by_category("Authentication")
        assert len(auth_opts) >= 2  # username and password
        for opt in auth_opts:
            assert opt.category == "Authentication"

    def test_scope_field_on_config_option(self) -> None:
        """Test that ConfigOption has scope field with valid values."""
        for opt in CONFIG_OPTIONS:
            assert hasattr(opt, "scope"), f"Option {opt.env_var} missing scope field"
            assert opt.scope in (
                "global",
                "project",
            ), f"Option {opt.env_var} has invalid scope: {opt.scope}"

    def test_global_scope_options(self) -> None:
        """Test that expected options have global scope."""
        global_opts = get_options_by_scope("global")
        global_env_vars = [opt.env_var for opt in global_opts]

        # API settings should be global
        assert "INSPIRE_BASE_URL" in global_env_vars
        assert "INSPIRE_BROWSER_API_PREFIX" in global_env_vars
        assert "INSPIRE_REQUESTS_HTTP_PROXY" in global_env_vars
        assert "INSPIRE_PLAYWRIGHT_PROXY" in global_env_vars

        # Password should remain global-scope for security defaults
        assert "INSPIRE_PASSWORD" in global_env_vars

    def test_project_scope_options(self) -> None:
        """Test that expected options have project scope."""
        project_opts = get_options_by_scope("project")
        project_env_vars = [opt.env_var for opt in project_opts]
        global_env_vars = [opt.env_var for opt in get_options_by_scope("global")]

        # Identity (username/password) lives at the active account only.
        # Switching accounts uses `inspire account use`, not a per-repo TOML.
        assert "INSPIRE_USERNAME" in global_env_vars
        assert "INSPIRE_PASSWORD" in global_env_vars
        assert "INSPIRE_USERNAME" not in project_env_vars
        assert "INSPIRE_PASSWORD" not in project_env_vars

        # Job/Notebook settings are project-scoped.
        assert "INSPIRE_SHM_SIZE" in project_env_vars
        assert "INSPIRE_JOB_ENABLE_NOTIFICATION" in project_env_vars
        assert "INSPIRE_NOTEBOOK_POST_START" in project_env_vars

    def test_get_options_by_scope(self) -> None:
        """Test get_options_by_scope helper function."""
        global_opts = get_options_by_scope("global")
        project_opts = get_options_by_scope("project")

        assert len(global_opts) > 0
        assert len(project_opts) > 0

        # All returned options should have correct scope
        for opt in global_opts:
            assert opt.scope == "global"
        for opt in project_opts:
            assert opt.scope == "project"

        # Together they should cover all options
        assert len(global_opts) + len(project_opts) == len(CONFIG_OPTIONS)


# ===========================================================================
# TOML loading tests
# ===========================================================================


class TestTomlLoading:
    """Tests for TOML config file loading."""

    def test_load_toml_basic(self, tmp_path: Path) -> None:
        """Test loading a basic TOML file."""
        toml_content = """
[auth]
username = "tomluser"

[api]
base_url = "https://custom.example.com"
"""
        config_file = tmp_path / "config.toml"
        config_file.write_text(toml_content)

        data = Config._load_toml(config_file)
        assert data["auth"]["username"] == "tomluser"
        assert data["api"]["base_url"] == "https://custom.example.com"

    def test_flatten_toml(self) -> None:
        """Test flattening nested TOML structure."""
        data = {
            "auth": {"username": "test", "password": "secret"},
            "api": {"base_url": "https://example.com"},
        }

        flat = Config._flatten_toml(data)

        assert flat["auth.username"] == "test"
        assert flat["auth.password"] == "secret"
        assert flat["api.base_url"] == "https://example.com"

    def test_toml_key_to_field(self) -> None:
        """Test mapping TOML keys to Config field names."""
        assert Config._toml_key_to_field("auth.username") == "username"
        assert Config._toml_key_to_field("api.base_url") == "base_url"
        assert Config._toml_key_to_field("proxy.requests_http") == "requests_http_proxy"
        assert Config._toml_key_to_field("proxy.playwright") == "playwright_proxy"
        assert Config._toml_key_to_field("job.shm_size") == "shm_size"
        assert (
            Config._toml_key_to_field("notebook.post_start")
            == "notebook_post_start"
        )
        assert Config._toml_key_to_field("nonexistent.key") is None


# ===========================================================================
# Layered config tests
# ===========================================================================


class TestLayeredConfig:
    """Tests for layered configuration loading."""

    @pytest.fixture(autouse=True)
    def _no_active_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Generator[None, None, None]:
        fake_home = tmp_path / "__home_no_account"
        fake_home.mkdir(exist_ok=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        yield

    @pytest.fixture
    def clean_env(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
        """Clear relevant env vars for testing."""
        env_vars = [
            "INSPIRE_USERNAME",
            "INSPIRE_PASSWORD",
            "INSPIRE_BASE_URL",
            "INSPIRE_REQUESTS_HTTP_PROXY",
            "INSPIRE_REQUESTS_HTTPS_PROXY",
            "INSPIRE_PLAYWRIGHT_PROXY",
            "INSPIRE_RTUNNEL_PROXY",
            "INSPIRE_JOB_AUTO_FAULT_TOLERANCE",
            "INSPIRE_JOB_FAULT_TOLERANCE_MAX_RETRY",
            "INSPIRE_JOB_ENABLE_NOTIFICATION",
            "INSPIRE_NOTEBOOK_POST_START",
        ]
        for var in env_vars:
            monkeypatch.delenv(var, raising=False)
        yield

    def test_from_files_and_env_defaults_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test config with only defaults (no files, no env)."""
        # Isolate from a real ~/.inspire/current on the dev machine.
        fake_home = tmp_path / "__home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.base_url == "https://qz.sii.edu.cn"
        assert cfg.job_fault_tolerance_max_retry == 10
        assert sources["base_url"] == SOURCE_DEFAULT
        assert sources["job_fault_tolerance_max_retry"] == SOURCE_DEFAULT

    def test_from_files_and_env_project_config_rejects_account_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Project config rejects account-scope keys with ConfigError.

        Identity / API / proxy keys live at the active account only; allowing
        them to flow from a per-repo file would silently let one repo poison
        another whenever the user `cd`s between them.
        """
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        (project_dir / "config.toml").write_text(
            '[auth]\nusername = "projectuser"\n'
            '[api]\nbase_url = "https://project.example.com"\n'
        )
        monkeypatch.chdir(tmp_path)

        with pytest.raises(ConfigError, match="account-scope keys"):
            Config.from_files_and_env(require_credentials=False)

    def test_from_files_and_env_project_config_accepts_project_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Project config loads project-scope keys."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        (project_dir / "config.toml").write_text(
            "[path_aliases]\nme = \"/inspire/test\"\n"
        )
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)
        assert cfg.path_aliases["me"] == "/inspire/test"
        assert sources["path_aliases"] == SOURCE_PROJECT

    def test_job_behavior_config_loads_and_env_overrides_notification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Job behavior options must survive the layered config field allowlist."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        (project_dir / "config.toml").write_text(
            "[job]\n"
            "auto_fault_tolerance = true\n"
            "fault_tolerance_max_retry = 6\n"
            "enable_notification = false\n"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_JOB_ENABLE_NOTIFICATION", "true")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.job_auto_fault_tolerance is True
        assert cfg.job_fault_tolerance_max_retry == 6
        assert cfg.job_enable_notification is True
        assert sources["job_auto_fault_tolerance"] == SOURCE_PROJECT
        assert sources["job_fault_tolerance_max_retry"] == SOURCE_PROJECT
        assert sources["job_enable_notification"] == SOURCE_ENV

    def test_from_files_and_env_loads_project_path_aliases(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        (project_dir / "config.toml").write_text(
            "[path_aliases]\n"
            'me = "/inspire/ssd/project/topic/alice/"\n'
            'qb-ilm2.public = "/inspire/qb-ilm2/project/topic/public/"\n'
        )
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.path_aliases["me"] == "/inspire/ssd/project/topic/alice/"
        assert cfg.path_aliases["qb-ilm2.public"] == "/inspire/qb-ilm2/project/topic/public/"
        assert sources["path_aliases"] == SOURCE_PROJECT

    def test_from_files_and_env_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Environment values override project TOML by default."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        (project_dir / "config.toml").write_text(
            '[notebook]\npost_start = "bash from-toml.sh"\n'
        )

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_USERNAME", "envuser")
        monkeypatch.setenv("INSPIRE_NOTEBOOK_POST_START", "bash from-env.sh")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.username == "envuser"
        assert cfg.notebook_post_start == "bash from-env.sh"
        assert sources["username"] == SOURCE_ENV
        assert sources["notebook_post_start"] == SOURCE_ENV

    def test_find_project_config_walks_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that project config search walks up directories."""
        # Create project structure: tmp/inspire/config.toml
        inspire_dir = tmp_path / ".inspire"
        inspire_dir.mkdir()
        config_file = inspire_dir / "config.toml"
        config_file.write_text('[notebook]\npost_start = "bash setup.sh"\n')

        # Work from a subdirectory: tmp/subdir/deep
        subdir = tmp_path / "subdir" / "deep"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)

        found = Config._find_project_config()

        assert found == config_file

class TestAccountConfigLayer:
    """Phase 4: per-account config at ``~/.inspire/accounts/<current>/config.toml``.

    All tests redirect ``Path.home()`` into ``tmp_path`` so the real
    ``~/.inspire/accounts/`` is never touched.
    """

    @pytest.fixture
    def clean_env(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
        for var in (
            "INSPIRE_USERNAME",
            "INSPIRE_PASSWORD",
            "INSPIRE_BASE_URL",
            "INSPIRE_NOTEBOOK_POST_START",
        ):
            monkeypatch.delenv(var, raising=False)
        yield

    @pytest.fixture
    def home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.chdir(tmp_path)
        return fake_home

    def _write_account_config(self, home: Path, name: str, body: str) -> Path:
        path = home / ".inspire" / "accounts" / name / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        (home / ".inspire" / "current").write_text(name + "\n")
        return path

    def _write_project_account_config(self, root: Path, name: str, body: str) -> Path:
        path = root / ".inspire" / "accounts" / name / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def test_account_config_drives_identity_when_active(
        self, home: Path, clean_env: None
    ) -> None:
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice-platform"\npassword = "pw"\n\n'
            '[api]\nbase_url = "https://alice.example.com"\n',
        )

        cfg, sources = Config.from_files_and_env(require_credentials=True)

        assert cfg.username == "alice-platform"
        assert cfg.password == "pw"
        assert cfg.base_url == "https://alice.example.com"
        assert sources["username"] == SOURCE_ACCOUNT
        assert sources["base_url"] == SOURCE_ACCOUNT

    def test_account_project_catalog_preserves_path_user(
        self, home: Path, clean_env: None
    ) -> None:
        self._write_account_config(
            home,
            "alice",
            '[project_catalog."CI-情境智能"]\n'
            'name = "CI-情境智能"\n'
            'path = "embodied-multimodality"\n'
            'path_user = "tongjingqi-CZXS25110029"\n',
        )

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.project_catalog["CI-情境智能"]["path_user"] == "tongjingqi-CZXS25110029"
        assert sources["project_catalog"] == SOURCE_ACCOUNT

    def test_account_path_aliases_load_as_defaults(
        self, home: Path, clean_env: None
    ) -> None:
        self._write_account_config(
            home,
            "alice",
            '[path_aliases]\n'
            'me = "/inspire/ssd/project/topic/alice/"\n'
            'public = "/inspire/ssd/project/topic/public/"\n',
        )

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.path_aliases["me"] == "/inspire/ssd/project/topic/alice/"
        assert cfg.path_aliases["public"] == "/inspire/ssd/project/topic/public/"
        assert sources["path_aliases"] == SOURCE_ACCOUNT

    def test_project_context_is_loaded_for_display(
        self, home: Path, clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice-platform"\npassword = "pw"\n',
        )
        monkeypatch.chdir(tmp_path)
        project_config = (
            tmp_path / ".inspire" / "accounts" / "alice" / "config.toml"
        )
        project_config.parent.mkdir(parents=True)
        project_config.write_text(
            '[context]\nproject = "CI-情境智能"\nworkspace = "CPU资源空间"\n',
            encoding="utf-8",
        )

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.context_project == "CI-情境智能"
        assert cfg.context_workspace == "CPU资源空间"
        assert sources["context_project"] == SOURCE_PROJECT
        assert sources["context_workspace"] == SOURCE_PROJECT

        monkeypatch.setattr(
            "inspire.config.workspaces.workspace_name_map",
            lambda _session: {},
        )
        monkeypatch.setattr(
            "inspire.platform.web.session.get_web_session",
            object,
        )
        result = CliRunner().invoke(cli_main, ["account", "context"])
        assert result.exit_code == 0
        assert result.output == (
            "active account=alice project=CI-情境智能 workspace=CPU资源空间\n"
            "account alice\n"
        )

    def test_shared_project_config_loads_with_active_account(
        self,
        home: Path,
        clean_env: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice-platform"\npassword = "pw"\n',
        )
        monkeypatch.delenv("INSPIRE_NOTEBOOK_POST_START", raising=False)
        shared_config = tmp_path / ".inspire" / "config.toml"
        shared_config.parent.mkdir(parents=True)
        shared_config.write_text(
            '[notebook]\npost_start = "bash shared.sh"\n'
            '[path_aliases]\npublic = "/inspire/ssd/project/topic/public/"\n',
            encoding="utf-8",
        )

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.notebook_post_start == "bash shared.sh"
        assert cfg.path_aliases["public"] == "/inspire/ssd/project/topic/public/"
        assert sources["notebook_post_start"] == SOURCE_PROJECT
        assert getattr(cfg, "_shared_project_config_path") == shared_config
        assert getattr(cfg, "_account_project_config_path") is None

    def test_account_project_config_overrides_shared_project_config(
        self,
        home: Path,
        clean_env: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice-platform"\npassword = "pw"\n',
        )
        monkeypatch.delenv("INSPIRE_NOTEBOOK_POST_START", raising=False)
        shared_config = tmp_path / ".inspire" / "config.toml"
        shared_config.parent.mkdir(parents=True)
        shared_config.write_text(
            '[notebook]\npost_start = "bash shared.sh"\n'
            '[path_aliases]\n'
            'me = "/inspire/ssd/project/topic/shared/"\n'
            'public = "/inspire/ssd/project/topic/public/"\n',
            encoding="utf-8",
        )
        account_config = self._write_project_account_config(
            tmp_path,
            "alice",
            '[notebook]\npost_start = "bash account.sh"\n'
            '[path_aliases]\nme = "/inspire/ssd/project/topic/alice/"\n',
        )

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.notebook_post_start == "bash account.sh"
        assert cfg.path_aliases["me"] == "/inspire/ssd/project/topic/alice/"
        assert cfg.path_aliases["public"] == "/inspire/ssd/project/topic/public/"
        assert sources["notebook_post_start"] == SOURCE_PROJECT
        assert getattr(cfg, "_shared_project_config_path") == shared_config
        assert getattr(cfg, "_account_project_config_path") == account_config

    def test_account_project_config_without_cli_keeps_shared_prefer_source(
        self,
        home: Path,
        clean_env: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice-platform"\npassword = "pw"\n',
        )
        monkeypatch.setenv("INSPIRE_NOTEBOOK_POST_START", "bash from-env.sh")
        shared_config = tmp_path / ".inspire" / "config.toml"
        shared_config.parent.mkdir(parents=True)
        shared_config.write_text(
            '[cli]\nprefer_source = "toml"\n'
            '[notebook]\npost_start = "bash shared.sh"\n',
            encoding="utf-8",
        )
        self._write_project_account_config(
            tmp_path,
            "alice",
            '[path_aliases]\nme = "/inspire/ssd/project/topic/alice/"\n',
        )

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.notebook_post_start == "bash shared.sh"
        assert cfg.prefer_source == "toml"
        assert sources["notebook_post_start"] == SOURCE_PROJECT

    def test_project_config_rejects_account_scope_keys(
        self, home: Path, clean_env: None, tmp_path: Path
    ) -> None:
        """Project config rejects account-scope keys and accepts project keys."""
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice-platform"\npassword = "pw"\n'
            '[api]\nbase_url = "https://alice.example.com"\n',
        )
        self._write_project_account_config(
            tmp_path,
            "alice",
            '[api]\nbase_url = "https://project.example.com"\n',
        )

        with pytest.raises(ConfigError, match="account-scope keys"):
            Config.from_files_and_env(require_credentials=False)

    def test_writable_config_path_targets_active_account(
        self, home: Path, clean_env: None
    ) -> None:
        """``inspire init`` writes to the active account's config.toml so the
        data it saves is the same file the loader then reads."""
        self._write_account_config(home, "alice", '[auth]\nusername = "a"\n')

        target = Config.writable_config_path()
        assert target == home / ".inspire" / "accounts" / "alice" / "config.toml"

    @pytest.mark.parametrize(
        "key_line,dotted_key",
        [
            ('[notebook]\npost_start = "bash setup.sh"', "notebook.post_start"),
        ],
    )
    def test_per_repo_keys_in_account_config_are_rejected(
        self, home: Path, clean_env: None, key_line: str, dotted_key: str
    ) -> None:
        """Every per-repo key must be flagged at account layer, not just paths.*."""
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice"\npassword = "pw"\n\n' + key_line + "\n",
        )

        with pytest.raises(ConfigError, match=re.escape(dotted_key)):
            Config.from_files_and_env(require_credentials=False)

    def test_account_config_proxy_merges_with_env_override(
        self, home: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[proxy].* loads from the account layer; INSPIRE_* env overrides one key."""
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice"\npassword = "pw"\n\n'
            '[proxy]\n'
            'requests_http = "http://127.0.0.1:7897"\n'
            'requests_https = "http://127.0.0.1:7897"\n'
            'playwright = "http://127.0.0.1:7897"\n'
            'rtunnel = "http://127.0.0.1:7897"\n',
        )
        monkeypatch.setenv("INSPIRE_REQUESTS_HTTP_PROXY", "http://127.0.0.1:17997")

        cfg, sources = Config.from_files_and_env(require_credentials=False)
        assert cfg.requests_http_proxy == "http://127.0.0.1:17997"
        assert cfg.requests_https_proxy == "http://127.0.0.1:7897"
        assert cfg.playwright_proxy == "http://127.0.0.1:7897"
        assert cfg.rtunnel_proxy == "http://127.0.0.1:7897"
        assert sources["requests_http_proxy"] == SOURCE_ENV
        assert sources["requests_https_proxy"] == SOURCE_ACCOUNT

    def test_remote_env_loads_from_account_layer(
        self, home: Path, clean_env: None
    ) -> None:
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice"\npassword = "pw"\n\n'
            '[remote_env]\nWANDB_API_KEY = "account-key"\nUV_PYTHON_INSTALL_DIR = "/opt/uv"\n',
        )
        cfg, sources = Config.from_files_and_env(require_credentials=False)
        assert cfg.remote_env == {
            "WANDB_API_KEY": "account-key",
            "UV_PYTHON_INSTALL_DIR": "/opt/uv",
        }
        assert sources["remote_env"] == SOURCE_ACCOUNT

    def test_remote_env_project_merges_with_account(
        self, home: Path, clean_env: None, tmp_path: Path
    ) -> None:
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice"\npassword = "pw"\n\n'
            '[remote_env]\nWANDB_API_KEY = "account-key"\nUV_PYTHON_INSTALL_DIR = "/opt/uv"\n',
        )
        self._write_project_account_config(
            tmp_path,
            "alice",
            '[remote_env]\nWANDB_API_KEY = "project-key"\nHF_TOKEN = "hf"\n',
        )

        cfg, sources = Config.from_files_and_env(require_credentials=False)
        assert cfg.remote_env == {
            "WANDB_API_KEY": "project-key",
            "UV_PYTHON_INSTALL_DIR": "/opt/uv",
            "HF_TOKEN": "hf",
        }
        assert sources["remote_env"] == SOURCE_PROJECT

    def test_project_config_is_scoped_by_active_account(
        self, home: Path, clean_env: None, tmp_path: Path
    ) -> None:
        """Switching accounts must not reuse workspace/path caches."""
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice"\npassword = "pw"\n',
        )
        self._write_account_config(
            home,
            "bob",
            '[auth]\nusername = "bob"\npassword = "pw"\n',
        )
        self._write_project_account_config(
            tmp_path,
            "alice",
            '[path_aliases]\nme = "/inspire/ssd/project/topic/alice/"\n',
        )
        self._write_project_account_config(
            tmp_path,
            "bob",
            '[path_aliases]\nme = "/inspire/ssd/project/topic/bob/"\n',
        )
        shared_project = tmp_path / ".inspire" / "config.toml"
        shared_project.write_text('[path_aliases]\nme = "/inspire/shared/"\n')

        (home / ".inspire" / "current").write_text("alice\n")
        alice_cfg, alice_sources = Config.from_files_and_env(require_credentials=False)

        (home / ".inspire" / "current").write_text("bob\n")
        bob_cfg, bob_sources = Config.from_files_and_env(require_credentials=False)

        assert alice_cfg.path_aliases["me"] == "/inspire/ssd/project/topic/alice/"
        assert bob_cfg.path_aliases["me"] == "/inspire/ssd/project/topic/bob/"
        assert alice_sources["path_aliases"] == SOURCE_PROJECT
        assert bob_sources["path_aliases"] == SOURCE_PROJECT

    def test_explicit_account_config_load_does_not_switch_active_account(
        self, home: Path, clean_env: None, tmp_path: Path
    ) -> None:
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice"\npassword = "pw"\n'
            '[api]\nbase_url = "https://alice.example.com"\n',
        )
        self._write_account_config(
            home,
            "bob",
            '[auth]\nusername = "bob"\npassword = "pw"\n'
            '[api]\nbase_url = "https://bob.example.com"\n',
        )
        self._write_project_account_config(
            tmp_path,
            "bob",
            '[path_aliases]\nme = "/inspire/ssd/project/topic/bob/"\n',
        )
        (home / ".inspire" / "current").write_text("alice\n")

        cfg, sources = Config.from_files_and_env(
            require_credentials=False,
            account="bob",
        )

        assert cfg.username == "bob"
        assert cfg.base_url == "https://bob.example.com"
        assert cfg.path_aliases["me"] == "/inspire/ssd/project/topic/bob/"
        assert sources["username"] == SOURCE_ACCOUNT
        assert sources["base_url"] == SOURCE_ACCOUNT
        assert sources["path_aliases"] == SOURCE_PROJECT
        assert (home / ".inspire" / "current").read_text() == "alice\n"

    def test_project_path_aliases_merge_over_account_defaults(
        self, home: Path, clean_env: None, tmp_path: Path
    ) -> None:
        self._write_account_config(
            home,
            "alice",
            '[path_aliases]\n'
            'me = "/inspire/ssd/project/topic/account/"\n'
            'public = "/inspire/ssd/project/topic/public/"\n',
        )
        self._write_project_account_config(
            tmp_path,
            "alice",
            '[path_aliases]\nme = "/inspire/qb-ilm2/project/topic/project/"\n',
        )

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.path_aliases["me"] == "/inspire/qb-ilm2/project/topic/project/"
        assert cfg.path_aliases["public"] == "/inspire/ssd/project/topic/public/"
        assert sources["path_aliases"] == SOURCE_PROJECT

    def test_project_config_search_does_not_treat_home_account_as_project(
        self, tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_home = tmp_path / "home"
        repo = fake_home / "repo"
        repo.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        account_config = fake_home / ".inspire" / "accounts" / "alice" / "config.toml"
        account_config.parent.mkdir(parents=True)
        account_config.write_text(
            '[auth]\nusername = "alice"\npassword = "pw"\n'
            '[api]\nbase_url = "https://alice.example.com"\n'
        )
        (fake_home / ".inspire" / "current").write_text("alice\n")
        monkeypatch.chdir(repo)

        cfg, sources = Config.from_files_and_env(require_credentials=False)
        account_path, project_path = Config.get_config_paths()

        assert cfg.username == "alice"
        assert cfg.base_url == "https://alice.example.com"
        assert sources["base_url"] == SOURCE_ACCOUNT
        assert account_path == account_config
        assert project_path is None

    def test_profile_write_targets_active_project_account_config(
        self, home: Path, clean_env: None, tmp_path: Path
    ) -> None:
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice"\npassword = "pw"\n',
        )

        result = CliRunner().invoke(
            cli_main,
            [
                "notebook",
                "profile",
                "set",
                "h200",
                "--workspace",
                "分布式训练空间",
                "--project",
                "CI-情境智能",
                "--group",
                "H200-2号机房",
                "--quota",
                "1,20,200",
                "--image",
                "unified-base:v2",
            ],
        )

        config_path = tmp_path / ".inspire" / "accounts" / "alice" / "config.toml"
        assert result.exit_code == 0, result.output
        assert config_path.exists()
        content = config_path.read_text(encoding="utf-8")
        assert "[profiles.notebook.h200]" in content
        assert 'workspace = "分布式训练空间"' in content

    def test_require_credentials_without_active_account_raises(
        self, home: Path, clean_env: None
    ) -> None:
        # Missing active-account credentials use one actionable error.
        with pytest.raises(ConfigError, match="Missing platform credentials"):
            Config.from_files_and_env(require_credentials=True)

    def test_get_config_paths_returns_account_and_project(
        self, home: Path, clean_env: None, tmp_path: Path
    ) -> None:
        self._write_account_config(
            home, "alice", '[auth]\nusername = "alice"\n'
        )
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "accounts" / "alice" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text('[notebook]\npost_start = "bash setup.sh"\n')

        account_path, proj_path = Config.get_config_paths()
        assert account_path == home / ".inspire" / "accounts" / "alice" / "config.toml"
        assert proj_path == project_config


# ===========================================================================
# Init command tests
# ===========================================================================


class TestInitCommand:
    """Tests for inspire init command."""

    def _project_config_path(self, root: Path) -> Path:
        return root / PROJECT_CONFIG_DIR / "accounts" / "default" / CONFIG_FILENAME

    def _account_config_path(self) -> Path:
        return Path.home() / ".inspire" / "accounts" / "default" / CONFIG_FILENAME

    @pytest.fixture
    def clean_env(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
        """Clear relevant env vars for testing."""
        # Clear all INSPIRE_* and INSP_* env vars
        for key in list(os.environ.keys()):
            if key.startswith("INSPIRE_") or key.startswith("INSP_"):
                monkeypatch.delenv(key, raising=False)
        yield

    @pytest.fixture(autouse=True)
    def _isolated_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Generator[None, None, None]:
        """Every init test gets an isolated fake ``~`` with a default active
        account, so ``Config.writable_config_path()`` resolves to a tmp path
        instead of the real user's ``~/.inspire/``.
        """
        fake_home = tmp_path / "__home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        account_dir = fake_home / ".inspire" / "accounts" / "default"
        account_dir.mkdir(parents=True)
        (account_dir / "config.toml").write_text("")
        (fake_home / ".inspire" / "current").write_text("default\n")
        yield

    def test_init_creates_template_when_no_env_vars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that init creates template config when no env vars detected."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        result = runner.invoke(init, ["--no-discover", "--scope", "project", "--force"])

        assert result.exit_code == 0
        assert result.output == "Configuration updated.\n"
        config_file = self._project_config_path(tmp_path)
        assert config_file.exists()
        content = config_file.read_text()
        assert "[auth]" not in content
        assert "[api]" not in content
        assert "[context]" in content
        assert "[path_aliases]" in content
        assert "[notebook]" in content

    def test_init_global_template_excludes_project_only_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """A global template must be loadable as an account config."""
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(init, ["--template", "--scope", "global", "--force"])

        assert result.exit_code == 0, result.output
        content = self._account_config_path().read_text()
        assert "[auth]" in content
        assert "[api]" in content
        assert "[tunnel]" in content
        assert "[context]" not in content
        assert "[notebook]" not in content
        assert "[path_aliases]" not in content
        assert "[profiles.notebook.example]" not in content
        Config.from_files_and_env(require_credentials=False)

    def test_init_project_template_excludes_account_scope_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """A project template must be loadable as a repo/account config."""
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(init, ["--template", "--scope", "project", "--force"])

        assert result.exit_code == 0, result.output
        content = self._project_config_path(tmp_path).read_text()
        assert "[auth]" not in content
        assert "[api]" not in content
        assert "[proxy]" not in content
        assert "[context]" in content
        assert "[notebook]" in content
        assert '# post_start = "bash /workspace/setup.sh"' in content
        Config.from_files_and_env(require_credentials=False)

    def test_init_project_env_file_writes_shared_project_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(
            init,
            ["--template", "--scope", "project", "--force", "--env-file", ".env"],
        )

        assert result.exit_code == 0, result.output
        account_project_config = self._project_config_path(tmp_path)
        shared_project_config = tmp_path / ".inspire" / "config.toml"
        assert account_project_config.exists()
        assert shared_project_config.exists()
        assert '[cli]\nenv_file = ".env"' in shared_project_config.read_text(encoding="utf-8")

    def test_init_template_flag_creates_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that --template flag creates template even with env vars."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")

        runner = CliRunner()
        result = runner.invoke(init, ["--template", "--scope", "project"])

        assert result.exit_code == 0
        assert result.output == "Configuration updated.\n"
        config_file = self._project_config_path(tmp_path)
        assert config_file.exists()
        content = config_file.read_text()
        # Should have project placeholders, not actual env var values
        assert "[context]" in content
        assert "[path_aliases]" in content
        assert "your_username" not in content
        assert "testuser" not in content

    def test_init_json_template_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that init uses the global --json output switch."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        result = runner.invoke(
            cli_main,
            ["--json", "init", "--template", "--scope", "project", "--force"],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["success"] is True
        assert payload["data"] == {"status": "updated"}
        assert str(tmp_path) not in result.output

    def test_init_json_fails_when_overwrite_prompt_would_be_needed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that JSON mode fails fast instead of entering interactive overwrite prompts."""
        monkeypatch.chdir(tmp_path)
        config_file = self._project_config_path(tmp_path)
        config_file.parent.mkdir(parents=True)
        config_file.write_text("[auth]\nusername = 'existing'")

        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            ["--json", "init", "--template", "--scope", "project"],
        )

        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["success"] is False
        assert payload["error"]["type"] == "ValidationError"
        assert "--force" in payload["error"]["message"]

    def test_init_warns_on_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that init warns when config exists."""
        from inspire.cli.commands.init import init_cmd as init_cmd_module

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(init_cmd_module, "_stdin_is_interactive", lambda: True)

        # Create existing config
        config_file = self._project_config_path(tmp_path)
        config_file.parent.mkdir(parents=True)
        config_file.write_text("[auth]\nusername = 'existing'")

        runner = CliRunner()
        # Decline the overwrite prompt for an explicit project template.
        result = runner.invoke(init, ["--template", "--scope", "project"], input="n\n")

        assert "already exists" in result.output
        assert "Configuration unchanged." in result.output
        # Original should be unchanged
        assert "existing" in config_file.read_text()

    def test_init_force_overwrites_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that --force overwrites existing config without prompting."""
        monkeypatch.chdir(tmp_path)

        # Create existing config
        config_file = self._project_config_path(tmp_path)
        config_file.parent.mkdir(parents=True)
        config_file.write_text("[auth]\nusername = 'existing'")

        runner = CliRunner()
        result = runner.invoke(init, ["--template", "--scope", "project", "--force"])

        assert result.exit_code == 0
        content = config_file.read_text()
        assert "existing" not in content
        assert "[context]" in content
        assert "[path_aliases]" in content

    def test_init_scope_project_writes_only_project_scope_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that --scope project does not write account-scope env vars."""
        monkeypatch.chdir(tmp_path)

        # Set both global and project scope env vars
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")  # global
        monkeypatch.setenv(
            "INSPIRE_NOTEBOOK_POST_START",
            "bash project-setup.sh",
        )

        runner = CliRunner()
        result = runner.invoke(init, ["--no-discover", "--scope", "project", "--force"])

        assert result.exit_code == 0

        # Project config should only carry the project-scope value.
        project_config = self._project_config_path(tmp_path)
        assert project_config.exists()
        project_content = project_config.read_text()
        assert 'username = "testuser"' not in project_content
        assert 'post_start = "bash project-setup.sh"' in project_content
        Config.from_files_and_env(require_credentials=False)

    def test_init_scope_global_writes_only_account_scope_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that --scope global does not write project-scope env vars."""
        monkeypatch.chdir(tmp_path)

        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_NOTEBOOK_POST_START", "bash project-setup.sh")

        result = CliRunner().invoke(init, ["--no-discover", "--scope", "global", "--force"])

        assert result.exit_code == 0, result.output
        account_content = self._account_config_path().read_text()
        assert 'username = "testuser"' in account_content
        assert "post_start" not in account_content
        Config.from_files_and_env(require_credentials=False)

    def test_init_scope_project_with_only_account_env_does_not_clobber(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        monkeypatch.chdir(tmp_path)
        self._account_config_path().write_text('[auth]\nusername = "existing"\n')
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")

        result = CliRunner().invoke(init, ["--no-discover", "--scope", "project", "--force"])

        assert result.exit_code == 0, result.output
        assert result.output == "Configuration unchanged.\n"
        assert not self._project_config_path(tmp_path).exists()
        assert 'username = "existing"' in self._account_config_path().read_text()

    def test_init_global_excludes_secrets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that init excludes secrets from config files."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "secretpass")

        runner = CliRunner()
        result = runner.invoke(init, ["--no-discover", "--scope", "global", "--force"])

        assert result.exit_code == 0
        content = self._account_config_path().read_text()

        # Username should be written
        assert 'username = "testuser"' in content
        # Password should be excluded (commented)
        assert "secretpass" not in content
        assert "# password - use env var INSPIRE_PASSWORD for security" in content

    def test_init_auto_split_only_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test auto-split with only project-scope env vars."""
        global_config = tmp_path / ".config" / "inspire" / "config.toml"
        monkeypatch.chdir(tmp_path)

        monkeypatch.setenv("INSPIRE_NOTEBOOK_POST_START", "bash project-setup.sh")

        runner = CliRunner()
        result = runner.invoke(init, ["--no-discover", "--scope", "project", "--force"])

        assert result.exit_code == 0

        # Project config should exist
        project_config = self._project_config_path(tmp_path)
        assert project_config.exists()
        project_content = project_config.read_text()
        assert 'post_start = "bash project-setup.sh"' in project_content

        # Global config should NOT exist (no global-scope vars)
        assert not global_config.exists()

    def test_default_path_aliases_use_platform_path_user(self) -> None:
        from inspire.cli.commands.init.discover import _default_path_aliases

        aliases = _default_path_aliases(
            project_topic="topic-a",
            selected_tier="ssd",
            path_user="tongjingqi-CZXS25110029",
        )

        assert aliases["me"] == "/inspire/ssd/project/topic-a/tongjingqi-CZXS25110029/"
        assert aliases["public"] == "/inspire/ssd/project/topic-a/public/"
        assert aliases["global-me"] == "/inspire/ssd/global_user/tongjingqi-CZXS25110029/"
        assert aliases["hdd.me"] == "/inspire/hdd/project/topic-a/tongjingqi-CZXS25110029/"
        assert aliases["ssd.public"] == "/inspire/ssd/project/topic-a/public/"
        assert aliases["qb-ilm2.me"] == (
            "/inspire/qb-ilm2/project/topic-a/tongjingqi-CZXS25110029/"
        )

    def test_default_path_aliases_without_personal_path_are_public_only(self) -> None:
        from inspire.cli.commands.init.discover import _default_path_aliases

        aliases = _default_path_aliases(
            project_topic="topic-a",
            selected_tier="ssd",
            path_user=None,
        )

        assert aliases["public"] == "/inspire/ssd/project/topic-a/public/"
        assert aliases["hdd.public"] == "/inspire/hdd/project/topic-a/public/"
        assert "me" not in aliases
        assert "global-me" not in aliases
        assert "hdd.me" not in aliases
        assert "hdd.global-me" not in aliases

    def test_discovered_path_aliases_use_platform_personal_directory(
        self,
    ) -> None:
        from inspire.cli.commands.init.discover import (
            _persist_default_path_aliases,
            _populate_project_catalog,
        )
        from inspire.platform.web.browser_api.files import FileDirectoryInfo
        from inspire.platform.web.browser_api.projects import ProjectInfo

        project = ProjectInfo(
            project_id="project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            name="CI-情境智能-探索课题",
            workspace_id="ws-11111111-1111-1111-1111-111111111111",
            en_name="exploration-topic",
        )
        project_catalog: dict[str, dict[str, str]] = {}

        class BrowserApi:
            @staticmethod
            def list_project_file_directories(**_: object) -> list[FileDirectoryInfo]:
                return [
                    FileDirectoryInfo(
                        directory="/inspire/hdd/project/exploration-topic/public",
                    ),
                    FileDirectoryInfo(
                        directory=(
                            "/inspire/hdd/project/exploration-topic/"
                            "tongjingqi-CZXS25110029"
                        ),
                    ),
                ]

        _populate_project_catalog(
            project_catalog=project_catalog,
            projects=[project],
            browser_api_module=BrowserApi,
            session=object(),
            workspace_id=project.workspace_id,
            force=True,
            project_alias_by_platform_id={project.project_id: project.name},
        )
        assert project_catalog[project.name]["path_user"] == "tongjingqi-CZXS25110029"
        project_data: dict[str, object] = {}

        _persist_default_path_aliases(
            project_data=project_data,
            selected_alias=project.name,
            project_catalog=project_catalog,
            selected_tier="ssd",
            force=True,
        )

        aliases = project_data["path_aliases"]
        assert aliases["me"] == (
            "/inspire/ssd/project/exploration-topic/tongjingqi-CZXS25110029/"
        )
        assert aliases["global-me"] == "/inspire/ssd/global_user/tongjingqi-CZXS25110029/"

    def test_project_catalog_discards_platform_id_key(self) -> None:
        from inspire.cli.commands.init.discover import _resolve_project_catalog_aliases
        from inspire.platform.web.browser_api.projects import ProjectInfo

        project = ProjectInfo(
            project_id="project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            name="Project One",
            workspace_id="ws-11111111-1111-1111-1111-111111111111",
        )
        global_data: dict[str, object] = {
            "projects": {"production": "Project One"},
            "project_catalog": {
                project.project_id: {
                    "name": "Project One",
                    "path": "topic",
                }
            },
        }

        project_alias_by_platform_id, project_catalog = _resolve_project_catalog_aliases(
            global_data=global_data,
            projects=[project],
        )

        assert project_alias_by_platform_id == {project.project_id: "production"}
        assert project_catalog == {}
        assert global_data["project_catalog"] == {}

    def test_project_catalog_never_falls_back_to_platform_id_key(self) -> None:
        from inspire.cli.commands.init.discover import _populate_project_catalog
        from inspire.platform.web.browser_api.projects import ProjectInfo

        project = ProjectInfo(
            project_id="project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            name="Project One",
            workspace_id="ws-11111111-1111-1111-1111-111111111111",
        )
        project_catalog: dict[str, dict[str, str]] = {}

        _populate_project_catalog(
            project_catalog=project_catalog,
            projects=[project],
            browser_api_module=object(),
            session=object(),
            workspace_id=project.workspace_id,
            force=True,
            project_alias_by_platform_id={},
        )

        assert project_catalog == {}

    def test_default_path_aliases_ignore_platform_id_catalog_key(self) -> None:
        from inspire.cli.commands.init.discover import _persist_default_path_aliases

        project_data: dict[str, object] = {}
        project_catalog = {
            "project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa": {
                "name": "Project One",
                "path": "topic",
                "path_user": "user-dir",
            }
        }

        _persist_default_path_aliases(
            project_data=project_data,
            selected_alias="Project One",
            project_catalog=project_catalog,
            selected_tier="ssd",
            force=True,
        )

        assert "path_aliases" not in project_data

    def test_project_catalog_uses_file_directory_api_for_personal_directory(
        self,
    ) -> None:
        from inspire.cli.commands.init.discover import _populate_project_catalog
        from inspire.platform.web.browser_api.files import FileDirectoryInfo
        from inspire.platform.web.browser_api.projects import ProjectInfo

        project = ProjectInfo(
            project_id="project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            name="CI-情境智能-探索课题",
            workspace_id="ws-11111111-1111-1111-1111-111111111111",
            en_name="exploration-topic",
        )
        project_catalog: dict[str, dict[str, str]] = {}
        calls: list[tuple[str, dict[str, object]]] = []

        class BrowserApi:
            @staticmethod
            def list_project_file_directories(**kwargs: object) -> list[FileDirectoryInfo]:
                calls.append(("list_project_file_directories", kwargs))
                return [
                    FileDirectoryInfo(
                        directory="/inspire/hdd/project/exploration-topic/public",
                    ),
                    FileDirectoryInfo(
                        directory=(
                            "/inspire/hdd/project/exploration-topic/"
                            "tongjingqi-CZXS25110029"
                        ),
                    ),
                ]

        _populate_project_catalog(
            project_catalog=project_catalog,
            projects=[project],
            browser_api_module=BrowserApi,
            session=object(),
            workspace_id=project.workspace_id,
            force=True,
            project_alias_by_platform_id={project.project_id: project.name},
        )

        assert [name for name, _ in calls] == ["list_project_file_directories"]
        assert project_catalog[project.name]["path"] == "exploration-topic"
        assert project_catalog[project.name]["path_user"] == "tongjingqi-CZXS25110029"

    def _setup_discover_mocks(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        get_web_session_side_effect=None,
        login_session=None,
        project_file_directories=None,
    ):
        """Wire up standard discover mocks and return (global_config, workspace_id)."""
        from inspire.platform.web.session.models import WebSession
        from inspire.platform.web.browser_api.availability.models import GPUAvailability
        from inspire.platform.web.browser_api.projects import ProjectInfo
        import inspire.platform.web.session as web_session_module
        import inspire.platform.web.browser_api as browser_api_module
        import inspire.platform.web.browser_api.workspaces as workspaces_module

        global_config = tmp_path / ".config" / "inspire" / "config.toml"
        monkeypatch.chdir(tmp_path)

        workspace_id = "ws-11111111-1111-1111-1111-111111111111"

        # Default session used by the fast path
        default_session = WebSession(
            storage_state={"cookies": [], "origins": []},
            created_at=0.0,
            workspace_id=workspace_id,
            login_username="cached-user",
            base_url="https://qz.sii.edu.cn",
            all_workspace_ids=[workspace_id],
            all_workspace_names={workspace_id: "CPU临时测试空间"},
        )

        if get_web_session_side_effect is not None:
            monkeypatch.setattr(
                web_session_module,
                "get_web_session",
                lambda **_: (_ for _ in ()).throw(get_web_session_side_effect),
            )
        else:
            monkeypatch.setattr(web_session_module, "get_web_session", lambda **_: default_session)

        if login_session is None:
            login_session = default_session
        monkeypatch.setattr(
            web_session_module,
            "login_with_playwright",
            lambda *a, **kw: login_session,
        )

        projects = [
            ProjectInfo(
                project_id="project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                name="My Project",
                workspace_id=workspace_id,
                en_name="exploration-topic",
            ),
        ]
        monkeypatch.setattr(browser_api_module, "list_projects", lambda **_: projects)
        monkeypatch.setattr(browser_api_module, "list_images", lambda **_: [])
        monkeypatch.setattr(
            browser_api_module,
            "list_compute_groups",
            lambda **_: [
                {
                    "logic_compute_group_id": "lcg-1",
                    "name": "H100 (CUDA 12.8)",
                }
            ],
        )
        monkeypatch.setattr(
            browser_api_module,
            "get_accurate_gpu_availability",
            lambda **_: [
                GPUAvailability(
                    group_id="lcg-1",
                    group_name="H100",
                    gpu_type="H100",
                    total_gpus=8,
                    used_gpus=0,
                    available_gpus=8,
                    low_priority_gpus=0,
                )
            ],
        )
        if project_file_directories is None:
            project_file_directories = []
        monkeypatch.setattr(
            browser_api_module,
            "list_project_file_directories",
            lambda **_: project_file_directories,
        )
        monkeypatch.setattr(workspaces_module, "try_enumerate_workspaces", lambda *_a, **_kw: [])

        # Stub out _ensure_playwright_browser and _ensure_ssh_key so they never
        # touch the real filesystem or try to launch a browser.
        from inspire.cli.commands.init import discover as discover_module

        monkeypatch.setattr(
            discover_module,
            "_ensure_playwright_browser",
            lambda **_kwargs: None,
        )
        monkeypatch.setattr(
            discover_module,
            "_ensure_ssh_key",
            lambda **_kwargs: None,
        )

        return global_config, workspace_id

    def test_discover_does_not_print_session_workspace_as_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        self._setup_discover_mocks(monkeypatch, tmp_path)
        monkeypatch.setenv("INSPIRE_USERNAME", "cached-user")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://example.invalid")

        runner = CliRunner()
        result = runner.invoke(init, ["--force"])

        assert result.exit_code == 0, result.output
        assert result.output == "Configuration updated.\n"
        assert "Discovering account catalog" not in result.output
        assert "Writing configuration files" not in result.output
        assert "Workspace:" not in result.output
        assert "CPU临时测试空间" not in result.output
        account_config = self._account_config_path()
        assert account_config.exists()
        account_content = account_config.read_text(encoding="utf-8")
        assert "[projects]" in account_content
        assert "[project_catalog" in account_content
        assert not self._project_config_path(tmp_path).exists()

    def test_discover_replaces_template_username_with_session_login(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        self._setup_discover_mocks(monkeypatch, tmp_path)
        self._account_config_path().write_text(
            '[auth]\nusername = "your_username"\n\n'
            '[api]\nbase_url = "https://api.example.com"\n',
            encoding="utf-8",
        )

        result = CliRunner().invoke(init, ["--force"])

        assert result.exit_code == 0, result.output
        assert result.output == "Configuration updated.\n"
        assert "Account: cached-user" not in result.output
        account_content = self._account_config_path().read_text(encoding="utf-8")
        assert 'username = "cached-user"' in account_content
        assert 'base_url = "https://qz.sii.edu.cn"' in account_content
        assert "your_username" not in account_content

    def test_global_scope_discover_writes_account_path_aliases(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        from inspire.platform.web.browser_api.files import FileDirectoryInfo

        self._setup_discover_mocks(
            monkeypatch,
            tmp_path,
            project_file_directories=[
                FileDirectoryInfo(
                    directory="/inspire/hdd/project/exploration-topic/public",
                ),
                FileDirectoryInfo(
                    directory=(
                        "/inspire/hdd/project/exploration-topic/"
                        "tongjingqi-CZXS25110029"
                    ),
                ),
            ],
        )
        monkeypatch.setenv("INSPIRE_USERNAME", "cached-user")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://example.invalid")

        result = CliRunner().invoke(init, ["--force"])

        assert result.exit_code == 0, result.output
        assert not self._project_config_path(tmp_path).exists()
        account_content = self._account_config_path().read_text(encoding="utf-8")
        assert "[path_aliases]" in account_content
        assert (
            'me = "/inspire/ssd/project/exploration-topic/tongjingqi-CZXS25110029/"'
            in account_content
        )
        assert 'public = "/inspire/ssd/project/exploration-topic/public/"' in account_content
        assert (
            'global-me = "/inspire/ssd/global_user/tongjingqi-CZXS25110029/"'
            in account_content
        )

    def test_project_scope_discover_writes_project_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        self._setup_discover_mocks(monkeypatch, tmp_path)
        monkeypatch.setenv("INSPIRE_USERNAME", "cached-user")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://example.invalid")

        runner = CliRunner()
        result = runner.invoke(init, ["--scope", "project", "--force"])

        assert result.exit_code == 0, result.output
        account_content = self._account_config_path().read_text(encoding="utf-8")
        assert "[projects]" in account_content
        assert "[project_catalog" in account_content

        project_config = self._project_config_path(tmp_path)
        assert project_config.exists()
        project_content = project_config.read_text(encoding="utf-8")
        assert "[context]" in project_content
        assert 'project = "My Project"' in project_content
        assert "[path_aliases]" not in project_content
        assert "workspace" not in project_content.lower()

    def test_project_scope_discover_writes_project_path_alias_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        from inspire.platform.web.browser_api.files import FileDirectoryInfo

        self._setup_discover_mocks(
            monkeypatch,
            tmp_path,
            project_file_directories=[
                FileDirectoryInfo(
                    directory="/inspire/hdd/project/exploration-topic/public",
                ),
                FileDirectoryInfo(
                    directory=(
                        "/inspire/hdd/project/exploration-topic/"
                        "tongjingqi-CZXS25110029"
                    ),
                ),
            ],
        )
        monkeypatch.setenv("INSPIRE_USERNAME", "cached-user")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://example.invalid")

        result = CliRunner().invoke(init, ["--scope", "project", "--force"])

        assert result.exit_code == 0, result.output
        account_content = self._account_config_path().read_text(encoding="utf-8")
        assert "[path_aliases]" in account_content
        project_content = self._project_config_path(tmp_path).read_text(encoding="utf-8")
        assert "[context]" in project_content
        assert "[path_aliases]" in project_content
        assert (
            'me = "/inspire/ssd/project/exploration-topic/tongjingqi-CZXS25110029/"'
            in project_content
        )
        assert 'public = "/inspire/ssd/project/exploration-topic/public/"' in project_content


# ===========================================================================
# Init helper function tests
# ===========================================================================


class TestInitHelpers:
    """Tests for init command helper functions."""

    @pytest.fixture
    def clean_env(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
        """Clear relevant env vars for testing."""
        for key in list(os.environ.keys()):
            if key.startswith("INSPIRE_") or key.startswith("INSP_"):
                monkeypatch.delenv(key, raising=False)
        yield

    def test_detect_env_vars(self, monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
        """Test detecting set environment variables."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://custom.example.com")

        detected = _detect_env_vars()

        env_vars = [opt.env_var for opt, _ in detected]
        assert "INSPIRE_USERNAME" in env_vars
        assert "INSPIRE_BASE_URL" in env_vars

    def test_detect_env_vars_empty(self, clean_env: None) -> None:
        """Test detecting no set environment variables."""
        detected = _detect_env_vars()
        assert len(detected) == 0

    def test_generate_toml_content(self, monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
        """Test TOML content generation."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://custom.example.com")
        monkeypatch.setenv("INSPIRE_TUNNEL_RETRIES", "5")

        detected = _detect_env_vars()
        toml_content = _generate_toml_content(detected)

        assert "[auth]" in toml_content
        assert 'username = "testuser"' in toml_content
        assert "[api]" in toml_content
        assert 'base_url = "https://custom.example.com"' in toml_content
        assert "[tunnel]" in toml_content
        assert "retries = 5" in toml_content

    def test_generate_toml_excludes_secrets(
        self, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that secrets are always excluded."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "secretpass")

        detected = _detect_env_vars()
        toml_content = _generate_toml_content(detected)

        assert 'username = "testuser"' in toml_content
        # Password should be commented out
        assert "# password - use env var INSPIRE_PASSWORD for security" in toml_content
        assert 'password = "secretpass"' not in toml_content

    def test_generate_toml_content_with_scope_filter(
        self, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test _generate_toml_content with scope_filter parameter."""
        # Set both global and project scope env vars
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://custom.example.com")  # global
        monkeypatch.setenv(
            "INSPIRE_NOTEBOOK_POST_START",
            "bash project-setup.sh",
        )

        detected = _detect_env_vars()

        # Generate with global filter
        global_content = _generate_toml_content(detected, scope_filter="global")
        assert 'base_url = "https://custom.example.com"' in global_content
        assert "post_start" not in global_content

        # Generate with project filter
        project_content = _generate_toml_content(detected, scope_filter="project")
        assert "base_url" not in project_content
        assert 'post_start = "bash project-setup.sh"' in project_content

        # Generate without filter (all options)
        all_content = _generate_toml_content(detected)
        assert 'base_url = "https://custom.example.com"' in all_content
        assert 'post_start = "bash project-setup.sh"' in all_content

    def test_generate_toml_preserves_special_chars(
        self, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that special characters in values are properly escaped."""
        monkeypatch.setenv("INSPIRE_BASE_URL", 'https://example.com/path?foo=bar&baz="test"')

        detected = _detect_env_vars()
        toml_content = _generate_toml_content(detected)

        # Value should be properly escaped
        assert 'base_url = "https://example.com/path?foo=bar&baz=\\"test\\""' in toml_content

# ===========================================================================
# Account show command tests
# ===========================================================================


class TestAccountShowCommand:
    """Tests for inspire account show."""

    @staticmethod
    def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep the view off the developer machine's real ~/.inspire."""
        fake_home = tmp_path / "__home"
        fake_home.mkdir(exist_ok=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.setattr("inspire.accounts.current_account", lambda: "test-account")
        monkeypatch.chdir(tmp_path)

    def test_account_show_reports_presence_without_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._isolate(tmp_path, monkeypatch)
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setenv(
            "INSPIRE_BASE_URL",
            "https://internal.example/private?token=secret",
        )

        runner = CliRunner()
        result = runner.invoke(account_command, ["show"])

        assert result.exit_code == 0, result.output
        assert "Account: test-account" in result.output
        assert "INSPIRE_USERNAME" in result.output
        assert "<configured>" in result.output
        assert "testuser" not in result.output
        assert "internal.example" not in result.output
        assert "[env]" not in result.output

        detailed = runner.invoke(account_command, ["show", "--details"])
        assert detailed.exit_code == 0, detailed.output
        assert "[env]" in detailed.output

    def test_account_show_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._isolate(tmp_path, monkeypatch)
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setenv(
            "INSPIRE_BASE_URL",
            "https://internal.example/private?token=secret",
        )

        result = CliRunner().invoke(cli_main, ["--json", "account", "show"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["success"] is True
        data = payload["data"]
        assert "config_file" not in data
        assert data["account"] == "test-account"
        assert data["values"]["INSPIRE_USERNAME"] == "<configured>"
        assert data["values"]["INSPIRE_PASSWORD"] == "********"
        assert "testuser" not in result.output
        assert "internal.example" not in result.output

    def test_account_show_omits_project_scope_options(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repository workload defaults belong to `config show`, not here."""
        self._isolate(tmp_path, monkeypatch)

        result = CliRunner().invoke(cli_main, ["--json", "account", "show", "--details"])

        assert result.exit_code == 0, result.output
        values = json.loads(result.output)["data"]["values"]
        assert "INSPIRE_BASE_URL" in values
        assert "INSPIRE_SHM_SIZE" not in values
        assert "INSPIRE_NOTEBOOK_POST_START" not in values

    def test_account_show_filter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._isolate(tmp_path, monkeypatch)
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")

        result = CliRunner().invoke(account_command, ["show", "--filter", "auth"])

        assert result.exit_code == 0, result.output
        assert "Authentication" in result.output

    def test_account_show_displays_effective_shell_proxy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Runtime shell proxies remain visible even when config proxy fields are unset."""
        self._isolate(tmp_path, monkeypatch)
        cfg = Config(username="", password="", base_url="https://qz.sii.edu.cn")
        monkeypatch.setattr(
            Config,
            "from_files_and_env",
            classmethod(lambda cls, **kwargs: (cfg, {})),
        )
        monkeypatch.setattr(
            Config,
            "get_config_paths",
            classmethod(lambda cls: (None, None)),
        )
        monkeypatch.delenv("http_proxy", raising=False)
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("https_proxy", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("all_proxy", raising=False)
        monkeypatch.delenv("ALL_PROXY", raising=False)
        monkeypatch.setenv(
            "HTTPS_PROXY",
            "http://alice:secret@proxy.example:18443/proxy-secret-path?token=value",
        )
        monkeypatch.setenv("NO_PROXY", ".example.org")

        result = CliRunner().invoke(account_command, ["show", "--filter", "Proxy"])

        assert result.exit_code == 0, result.output
        assert "Effective runtime proxy" in result.output
        assert "source=system_env" in result.output
        assert "route=proxy" in result.output
        assert "NO_PROXY=not_matched" in result.output
        assert "configured_proxy=" not in result.output
        assert "proxy.example" not in result.output
        assert "18443" not in result.output
        assert "alice" not in result.output
        assert "secret" not in result.output
        assert "proxy-secret-path" not in result.output
        assert "token" not in result.output

    def test_account_show_json_includes_effective_proxy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._isolate(tmp_path, monkeypatch)
        cfg = Config(username="", password="", base_url="https://qz.sii.edu.cn")
        monkeypatch.setattr(
            Config,
            "from_files_and_env",
            classmethod(lambda cls, **kwargs: (cfg, {})),
        )
        monkeypatch.setattr(
            Config,
            "get_config_paths",
            classmethod(lambda cls: (None, None)),
        )
        monkeypatch.delenv("http_proxy", raising=False)
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("https_proxy", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("all_proxy", raising=False)
        monkeypatch.delenv("ALL_PROXY", raising=False)
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:18443")

        result = CliRunner().invoke(cli_main, ["--json", "account", "show", "--filter", "Proxy"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert "values" in data
        assert "target" not in data["effective_proxy"]
        assert data["effective_proxy"]["requests"]["source"] == "system_env"
        assert data["effective_proxy"]["playwright"]["source"] == "requests:system_env"
        assert "proxy.example" not in result.output
        assert "qz.sii.edu.cn" not in result.output

    def test_account_show_redacts_configured_proxy_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._isolate(tmp_path, monkeypatch)
        cfg = Config(
            username="",
            password="",
            base_url="https://qz.sii.edu.cn",
            requests_https_proxy=(
                "http://proxy-user:proxy-password@proxy.example:18443/secret-path?token=value"
            ),
        )
        monkeypatch.setattr(
            Config,
            "from_files_and_env",
            classmethod(lambda cls, **kwargs: (cfg, {"requests_https_proxy": SOURCE_ACCOUNT})),
        )
        monkeypatch.setattr(
            Config,
            "get_config_paths",
            classmethod(lambda cls: (None, None)),
        )

        result = CliRunner().invoke(account_command, ["show", "--filter", "Proxy"])

        assert result.exit_code == 0, result.output
        assert "INSPIRE_REQUESTS_HTTPS_PROXY" in result.output
        assert "<configured>" in result.output
        assert "proxy-user" not in result.output
        assert "proxy-password" not in result.output
        assert "proxy.example" not in result.output
        assert "18443" not in result.output
        assert "secret-path" not in result.output
        assert "token" not in result.output


# ===========================================================================
# Config show command tests
# ===========================================================================


class TestConfigShowCommand:
    """Tests for inspire config show – repository workload defaults only."""

    @staticmethod
    def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = tmp_path / "__home"
        fake_home.mkdir(exist_ok=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.chdir(tmp_path)

    def test_config_show_reports_configured_project_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._isolate(tmp_path, monkeypatch)
        monkeypatch.setenv("INSPIRE_SHM_SIZE", "64")

        runner = CliRunner()
        result = runner.invoke(config_command, ["show"])

        assert result.exit_code == 0, result.output
        assert "INSPIRE_SHM_SIZE" in result.output
        assert "64" in result.output
        assert "[env]" not in result.output

        detailed = runner.invoke(config_command, ["show", "--details"])
        assert detailed.exit_code == 0, detailed.output
        assert "[env]" in detailed.output
        assert "Precedence: environment wins" in detailed.output

    def test_config_show_says_so_when_nothing_is_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._isolate(tmp_path, monkeypatch)
        for var in (
            "INSPIRE_SHM_SIZE",
            "INSPIRE_JOB_AUTO_FAULT_TOLERANCE",
            "INSPIRE_JOB_FAULT_TOLERANCE_MAX_RETRY",
            "INSPIRE_JOB_ENABLE_NOTIFICATION",
            "INSPIRE_NOTEBOOK_POST_START",
        ):
            monkeypatch.delenv(var, raising=False)

        result = CliRunner().invoke(config_command, ["show"])

        assert result.exit_code == 0, result.output
        assert "No workload defaults configured" in result.output

    def test_config_show_omits_account_scope_options(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identity, API, and proxy belong to `account show`, not here."""
        self._isolate(tmp_path, monkeypatch)
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv(
            "INSPIRE_BASE_URL",
            "https://internal.example/private?token=secret",
        )

        result = CliRunner().invoke(cli_main, ["--json", "config", "show", "--details"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        values = data["values"]
        assert "INSPIRE_SHM_SIZE" in values
        assert "INSPIRE_USERNAME" not in values
        assert "INSPIRE_BASE_URL" not in values
        assert "INSPIRE_REQUESTS_HTTPS_PROXY" not in values
        assert "effective_proxy" not in data
        assert "testuser" not in result.output
        assert "internal.example" not in result.output


# ===========================================================================
# Project dotenv tests
# ===========================================================================


class TestProjectEnvFile:
    """The `[cli] env_file` layer registered by `inspire init --env-file`."""

    def test_cli_loads_project_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_home = tmp_path / "__home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("INSPIRE_JOB_FAULT_TOLERANCE_MAX_RETRY", raising=False)

        (tmp_path / ".env").write_text(
            "INSPIRE_JOB_FAULT_TOLERANCE_MAX_RETRY=123\n",
            encoding="utf-8",
        )
        project_config = tmp_path / ".inspire" / "config.toml"
        project_config.parent.mkdir()
        project_config.write_text('[cli]\nenv_file = ".env"\n', encoding="utf-8")

        result = CliRunner().invoke(
            cli_main,
            ["--json", "config", "show", "--details"],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert "env_file" not in data
        assert data["env_file_present"] is True
        assert str(tmp_path / ".env") not in result.output
        assert data["values"]["INSPIRE_JOB_FAULT_TOLERANCE_MAX_RETRY"]["value"] == "123"
        assert (
            data["values"]["INSPIRE_JOB_FAULT_TOLERANCE_MAX_RETRY"]["source"]
            == SOURCE_ENV_FILE
        )

    def test_real_env_overrides_project_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_home = tmp_path / "__home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_JOB_FAULT_TOLERANCE_MAX_RETRY", "456")

        (tmp_path / ".env").write_text(
            "INSPIRE_JOB_FAULT_TOLERANCE_MAX_RETRY=123\n",
            encoding="utf-8",
        )
        project_config = tmp_path / ".inspire" / "config.toml"
        project_config.parent.mkdir()
        project_config.write_text('[cli]\nenv_file = ".env"\n', encoding="utf-8")

        result = CliRunner().invoke(
            cli_main,
            ["--json", "config", "show", "--details"],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["values"]["INSPIRE_JOB_FAULT_TOLERANCE_MAX_RETRY"]["value"] == "456"
        assert (
            data["values"]["INSPIRE_JOB_FAULT_TOLERANCE_MAX_RETRY"]["source"]
            == SOURCE_ENV
        )


# ===========================================================================
# prefer_source tests
# ===========================================================================


class TestPreferSource:
    """Tests for the [cli] prefer_source config setting."""

    @pytest.fixture(autouse=True)
    def _no_active_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Generator[None, None, None]:
        fake_home = tmp_path / "__home_no_account"
        fake_home.mkdir(exist_ok=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        yield

    @pytest.fixture
    def clean_env(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
        """Clear relevant env vars for testing."""
        env_vars = [
            "INSPIRE_USERNAME",
            "INSPIRE_PASSWORD",
            "INSPIRE_BASE_URL",
            "INSPIRE_REQUESTS_HTTP_PROXY",
            "INSPIRE_REQUESTS_HTTPS_PROXY",
            "INSPIRE_PLAYWRIGHT_PROXY",
            "INSPIRE_RTUNNEL_PROXY",
            "INSPIRE_JOB_ENABLE_NOTIFICATION",
            "INSPIRE_NOTEBOOK_POST_START",
        ]
        for var in env_vars:
            monkeypatch.delenv(var, raising=False)
        yield

    def test_default_env_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """env vars override project TOML by default (project-scope key)."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        (project_dir / "config.toml").write_text(
            '[notebook]\npost_start = "bash from-toml.sh"\n'
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_NOTEBOOK_POST_START", "bash from-env.sh")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.notebook_post_start == "bash from-env.sh"
        assert sources["notebook_post_start"] == SOURCE_ENV
        assert cfg.prefer_source == "env"

    def test_prefer_source_env_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """prefer_source = 'env' lets env vars win (project-scope key)."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        (project_dir / "config.toml").write_text(
            '[cli]\nprefer_source = "env"\n'
            '[notebook]\npost_start = "bash from-toml.sh"\n'
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_NOTEBOOK_POST_START", "bash from-env.sh")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.notebook_post_start == "bash from-env.sh"
        assert sources["notebook_post_start"] == SOURCE_ENV

    def test_prefer_source_toml_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """prefer_source = 'toml' keeps TOML values over env vars (project-scope key)."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        (project_dir / "config.toml").write_text(
            '[cli]\nprefer_source = "toml"\n'
            '[notebook]\npost_start = "bash from-toml.sh"\n'
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_NOTEBOOK_POST_START", "bash from-env.sh")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.notebook_post_start == "bash from-toml.sh"
        assert sources["notebook_post_start"] == SOURCE_PROJECT
        assert cfg.prefer_source == "toml"

    def test_prefer_source_toml_env_fills_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """prefer_source = 'toml' still picks up env vars for fields NOT in project TOML."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        (project_dir / "config.toml").write_text(
            '[cli]\nprefer_source = "toml"\n'
            '[notebook]\npost_start = "bash from-toml.sh"\n'
        )
        monkeypatch.chdir(tmp_path)
        # Set env var for a field NOT in the project TOML
        monkeypatch.setenv("INSPIRE_JOB_ENABLE_NOTIFICATION", "true")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.notebook_post_start == "bash from-toml.sh"
        assert sources["notebook_post_start"] == SOURCE_PROJECT
        assert cfg.job_enable_notification is True
        assert sources["job_enable_notification"] == SOURCE_ENV

    def test_prefer_source_toml_global_still_overridden_by_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that prefer_source = 'toml' only protects project TOML, not global TOML."""
        home = Path.home()
        account_config = home / ".inspire" / "accounts" / "alice" / "config.toml"
        account_config.parent.mkdir(parents=True)
        account_config.write_text(
            '[api]\nbase_url = "https://account.example.com"\n'
        )
        (home / ".inspire" / "current").write_text("alice\n")

        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[cli]
prefer_source = "toml"
"""
        )

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://env.example.com")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.base_url == "https://env.example.com"
        assert sources["base_url"] == SOURCE_ENV

    def test_prefer_source_invalid_raises_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that an invalid prefer_source value raises ConfigError."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[cli]
prefer_source = "invalid"
"""
        )
        monkeypatch.chdir(tmp_path)

        with pytest.raises(ConfigError, match="Invalid prefer_source value"):
            Config.from_files_and_env(require_credentials=False)

    def test_config_show_displays_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that config show displays the precedence mode."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[cli]
prefer_source = "toml"
"""
        )
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(config_command, ["show", "--details"])

        assert result.exit_code == 0
        assert "Precedence:" in result.output
        assert "project TOML wins" in result.output

    def test_config_show_displays_default_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that config show displays default precedence when no prefer_source set."""
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(config_command, ["show", "--details"])

        assert result.exit_code == 0
        assert "Precedence:" in result.output
        assert "environment wins" in result.output

    def test_config_show_json_includes_prefer_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """The root JSON mode includes prefer_source."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[cli]
prefer_source = "toml"
"""
        )
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            ["--json", "config", "show", "--details"],
        )

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["prefer_source"] == "toml"
