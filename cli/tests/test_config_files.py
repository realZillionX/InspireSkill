"""Tests for TOML config file loading and layered configuration."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Generator

import pytest
from click.testing import CliRunner

from inspire.config import (
    Config,
    ConfigError,
    SOURCE_DEFAULT,
    SOURCE_GLOBAL,
    SOURCE_PROJECT,
    SOURCE_ENV,
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
from inspire.cli.commands.init import (
    init,
    _detect_env_vars,
    _derive_shared_path_group,
    _generate_toml_content,
)
from inspire.cli.commands.config import config as config_command

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
        assert "INSPIRE_TIMEOUT" in global_env_vars
        assert "INSPIRE_REQUESTS_HTTP_PROXY" in global_env_vars
        assert "INSPIRE_PLAYWRIGHT_PROXY" in global_env_vars

        # GitHub server and token should be global
        assert "INSP_GITHUB_SERVER" in global_env_vars
        assert "INSP_GITHUB_TOKEN" in global_env_vars

        # SSH paths should be global
        assert "INSPIRE_RTUNNEL_DOWNLOAD_URL" in global_env_vars

        # Password should remain global-scope for security defaults
        assert "INSPIRE_PASSWORD" in global_env_vars

    def test_project_scope_options(self) -> None:
        """Test that expected options have project scope."""
        project_opts = get_options_by_scope("project")
        project_env_vars = [opt.env_var for opt in project_opts]

        # Username should be project-scoped (different repos can use different accounts)
        assert "INSPIRE_USERNAME" in project_env_vars
        assert "INSPIRE_PASSWORD" not in project_env_vars

        # Paths like target_dir should be project
        assert "INSPIRE_TARGET_DIR" in project_env_vars
        assert "INSPIRE_LOG_PATTERN" in project_env_vars

        # GitHub repo should be project
        assert "INSP_GITHUB_REPO" in project_env_vars

        # Job/Notebook settings should be project
        assert "INSP_PRIORITY" in project_env_vars
        assert "INSPIRE_NOTEBOOK_RESOURCE" in project_env_vars

        # Bridge/Sync settings should be project
        assert "INSPIRE_BRIDGE_DENYLIST" in project_env_vars
        assert "INSPIRE_DEFAULT_REMOTE" in project_env_vars

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
timeout = 60
"""
        config_file = tmp_path / "config.toml"
        config_file.write_text(toml_content)

        data = Config._load_toml(config_file)
        assert data["auth"]["username"] == "tomluser"
        assert data["api"]["base_url"] == "https://custom.example.com"
        assert data["api"]["timeout"] == 60

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
        assert Config._toml_key_to_field("api.timeout") == "timeout"
        assert Config._toml_key_to_field("proxy.requests_http") == "requests_http_proxy"
        assert Config._toml_key_to_field("proxy.playwright") == "playwright_proxy"
        assert Config._toml_key_to_field("paths.target_dir") == "target_dir"
        assert Config._toml_key_to_field("workspaces.cpu") == "workspace_cpu_id"
        assert Config._toml_key_to_field("workspaces.gpu") == "workspace_gpu_id"
        assert Config._toml_key_to_field("workspaces.internet") == "workspace_internet_id"
        assert Config._toml_key_to_field("nonexistent.key") is None


# ===========================================================================
# Layered config tests
# ===========================================================================


class TestLayeredConfig:
    """Tests for layered configuration loading."""

    @pytest.fixture
    def clean_env(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
        """Clear relevant env vars for testing."""
        env_vars = [
            "INSPIRE_USERNAME",
            "INSPIRE_PASSWORD",
            "INSPIRE_BASE_URL",
            "INSPIRE_TIMEOUT",
            "INSPIRE_REQUESTS_HTTP_PROXY",
            "INSPIRE_REQUESTS_HTTPS_PROXY",
            "INSPIRE_PLAYWRIGHT_PROXY",
            "INSPIRE_RTUNNEL_PROXY",
            "INSPIRE_TARGET_DIR",
            "INSP_GITHUB_SERVER",
        ]
        for var in env_vars:
            monkeypatch.delenv(var, raising=False)
        yield

    def test_from_files_and_env_defaults_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test config with only defaults (no files, no env)."""
        # Point to non-existent config paths
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.base_url == "https://api.example.com"
        assert cfg.timeout == 30
        assert sources["base_url"] == SOURCE_DEFAULT
        assert sources["timeout"] == SOURCE_DEFAULT

    def test_from_files_and_env_global_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test loading values from global config."""
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text(
            """
[auth]
username = "globaluser"

[api]
base_url = "https://global.example.com"
timeout = 45
"""
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.username == "globaluser"
        assert cfg.base_url == "https://global.example.com"
        assert cfg.timeout == 45
        assert sources["username"] == SOURCE_GLOBAL
        assert sources["base_url"] == SOURCE_GLOBAL

    def test_from_files_and_env_project_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test loading values from project config."""
        # Create project config
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[auth]
username = "projectuser"

[api]
timeout = 120
"""
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.username == "projectuser"
        assert cfg.timeout == 120
        assert sources["username"] == SOURCE_PROJECT
        assert sources["timeout"] == SOURCE_PROJECT

    def test_from_files_and_env_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that project config overrides global config."""
        # Create global config
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text(
            """
[auth]
username = "globaluser"

[api]
timeout = 45
base_url = "https://global.example.com"
"""
        )

        # Create project config
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[api]
timeout = 120
"""
        )

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        # Username from global
        assert cfg.username == "globaluser"
        assert sources["username"] == SOURCE_GLOBAL

        # base_url from global
        assert cfg.base_url == "https://global.example.com"
        assert sources["base_url"] == SOURCE_GLOBAL

        # timeout overridden by project
        assert cfg.timeout == 120
        assert sources["timeout"] == SOURCE_PROJECT

    def test_from_files_and_env_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that env vars override config files."""
        # Create global config
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text(
            """
[auth]
username = "globaluser"

[api]
timeout = 45
"""
        )

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_USERNAME", "envuser")
        monkeypatch.setenv("INSPIRE_TIMEOUT", "90")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        # Env vars should override
        assert cfg.username == "envuser"
        assert cfg.timeout == 90
        assert sources["username"] == SOURCE_ENV
        assert sources["timeout"] == SOURCE_ENV

    def test_from_files_and_env_proxy_layering(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Proxy settings should load from [proxy] and allow env override."""
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text(
            """
[auth]
username = "globaluser"

[proxy]
requests_http = "http://127.0.0.1:7897"
requests_https = "http://127.0.0.1:7897"
playwright = "http://127.0.0.1:7897"
rtunnel = "http://127.0.0.1:7897"
"""
        )

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_REQUESTS_HTTP_PROXY", "http://127.0.0.1:17997")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.requests_http_proxy == "http://127.0.0.1:17997"
        assert cfg.requests_https_proxy == "http://127.0.0.1:7897"
        assert cfg.playwright_proxy == "http://127.0.0.1:7897"
        assert cfg.rtunnel_proxy == "http://127.0.0.1:7897"
        assert sources["requests_http_proxy"] == SOURCE_ENV
        assert sources["requests_https_proxy"] == SOURCE_GLOBAL
        assert sources["playwright_proxy"] == SOURCE_GLOBAL
        assert sources["rtunnel_proxy"] == SOURCE_GLOBAL

    def test_from_files_and_env_remote_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test loading remote_env section from config files."""
        # Create global config with remote_env
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text(
            """
[auth]
username = "testuser"

[remote_env]
WANDB_API_KEY = "global-key"
UV_PYTHON_INSTALL_DIR = "/path/to/uv"
"""
        )

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.remote_env == {
            "WANDB_API_KEY": "global-key",
            "UV_PYTHON_INSTALL_DIR": "/path/to/uv",
        }
        assert sources["remote_env"] == SOURCE_GLOBAL

    def test_from_files_and_env_remote_env_project_merges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that project remote_env merges with global."""
        # Create global config
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text(
            """
[remote_env]
WANDB_API_KEY = "global-key"
UV_PYTHON_INSTALL_DIR = "/path/to/uv"
"""
        )

        # Create project config with different remote_env
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[remote_env]
WANDB_API_KEY = "project-key"
HF_TOKEN = "hf-token"
"""
        )

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        # Project should override WANDB_API_KEY and add HF_TOKEN
        # UV_PYTHON_INSTALL_DIR from global should remain
        assert cfg.remote_env == {
            "WANDB_API_KEY": "project-key",
            "UV_PYTHON_INSTALL_DIR": "/path/to/uv",
            "HF_TOKEN": "hf-token",
        }
        assert sources["remote_env"] == SOURCE_PROJECT

    def test_find_project_config_walks_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that project config search walks up directories."""
        # Create project structure: tmp/inspire/config.toml
        inspire_dir = tmp_path / ".inspire"
        inspire_dir.mkdir()
        config_file = inspire_dir / "config.toml"
        config_file.write_text("[api]\ntimeout = 77")

        # Work from a subdirectory: tmp/subdir/deep
        subdir = tmp_path / "subdir" / "deep"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)

        found = Config._find_project_config()

        assert found == config_file

    def test_from_files_and_env_require_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test error when credentials required but missing."""
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(ConfigError, match="Missing username"):
            Config.from_files_and_env(require_credentials=True)

    def test_get_config_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test get_config_paths returns correct paths."""
        # Create global config
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text("[api]\ntimeout = 1")

        # Create project config
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text("[api]\ntimeout = 2")

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        global_path, project_path = Config.get_config_paths()

        assert global_path == global_config
        assert project_path == project_config

    def test_get_config_paths_respects_global_config_path_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test env override for global config path is honored."""
        default_global = tmp_path / "default-global" / "config.toml"
        default_global.parent.mkdir(parents=True)
        default_global.write_text("[api]\ntimeout = 1")

        isolated_global = tmp_path / "isolated-global" / "config.toml"
        isolated_global.parent.mkdir(parents=True)
        isolated_global.write_text("[api]\ntimeout = 2")

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", default_global)
        monkeypatch.setenv("INSPIRE_GLOBAL_CONFIG_PATH", str(isolated_global))
        monkeypatch.chdir(tmp_path)

        global_path, project_path = Config.get_config_paths()

        assert global_path == isolated_global
        assert project_path is None

    def test_from_files_and_env_respects_global_config_path_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test loader reads global config from env-overridden path."""
        default_global = tmp_path / "default-global" / "config.toml"
        default_global.parent.mkdir(parents=True)
        default_global.write_text("[api]\ntimeout = 1")

        isolated_global = tmp_path / "isolated-global" / "config.toml"
        isolated_global.parent.mkdir(parents=True)
        isolated_global.write_text("[api]\ntimeout = 77")

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", default_global)
        monkeypatch.setenv("INSPIRE_GLOBAL_CONFIG_PATH", str(isolated_global))
        monkeypatch.chdir(tmp_path)

        cfg, _ = Config.from_files_and_env(require_credentials=False)
        assert cfg.timeout == 77


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
            "INSPIRE_TIMEOUT",
            "INSPIRE_TARGET_DIR",
            "INSPIRE_GLOBAL_CONFIG_PATH",
        ):
            monkeypatch.delenv(var, raising=False)
        yield

    @pytest.fixture
    def home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        # Point the legacy global path into tmp — absent by default, so the
        # account layer is the only TOML source unless a test writes to it.
        monkeypatch.setattr(
            Config, "GLOBAL_CONFIG_PATH", fake_home / ".config" / "inspire" / "config.toml"
        )
        monkeypatch.chdir(tmp_path)
        return fake_home

    def _write_account_config(self, home: Path, name: str, body: str) -> Path:
        path = home / ".inspire" / "accounts" / name / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        (home / ".inspire" / "current").write_text(name + "\n")
        return path

    def test_account_config_drives_identity_when_active(
        self, home: Path, clean_env: None
    ) -> None:
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice-platform"\npassword = "pw"\n\n'
            '[api]\nbase_url = "https://alice.example.com"\ntimeout = 55\n',
        )

        cfg, sources = Config.from_files_and_env(require_credentials=True)

        assert cfg.username == "alice-platform"
        assert cfg.password == "pw"
        assert cfg.base_url == "https://alice.example.com"
        assert cfg.timeout == 55
        assert sources["username"] == SOURCE_GLOBAL
        assert sources["base_url"] == SOURCE_GLOBAL

    def test_account_layer_replaces_legacy_global_layer(
        self, home: Path, clean_env: None
    ) -> None:
        # Legacy global says one thing; active account config says another.
        legacy_global = home / ".config" / "inspire" / "config.toml"
        legacy_global.parent.mkdir(parents=True, exist_ok=True)
        legacy_global.write_text('[auth]\nusername = "legacy-user"\n[api]\ntimeout = 10\n')

        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice-wins"\npassword = "pw"\n[api]\ntimeout = 99\n',
        )

        cfg, _ = Config.from_files_and_env(require_credentials=False)

        assert cfg.username == "alice-wins"
        assert cfg.timeout == 99  # account layer, not legacy global

    def test_legacy_global_used_when_no_active_account(
        self, home: Path, clean_env: None
    ) -> None:
        legacy_global = home / ".config" / "inspire" / "config.toml"
        legacy_global.parent.mkdir(parents=True, exist_ok=True)
        legacy_global.write_text('[auth]\nusername = "legacy-user"\npassword = "pw"\n')
        # NOTE: no ~/.inspire/current written => no active account.

        cfg, sources = Config.from_files_and_env(require_credentials=True)

        assert cfg.username == "legacy-user"
        assert cfg.password == "pw"
        assert sources["username"] == SOURCE_GLOBAL

    def test_account_config_missing_falls_back_to_legacy_global(
        self, home: Path, clean_env: None
    ) -> None:
        """User set ``inspire account use alice`` but never wrote the config.
        The loader should not crash — it falls through to legacy global."""
        (home / ".inspire" / "current").parent.mkdir(parents=True, exist_ok=True)
        (home / ".inspire" / "current").write_text("alice\n")

        legacy_global = home / ".config" / "inspire" / "config.toml"
        legacy_global.parent.mkdir(parents=True, exist_ok=True)
        legacy_global.write_text('[auth]\nusername = "legacy-user"\npassword = "pw"\n')

        cfg, _ = Config.from_files_and_env(require_credentials=False)
        assert cfg.username == "legacy-user"

    def test_project_config_still_overrides_account_config(
        self, home: Path, clean_env: None, tmp_path: Path
    ) -> None:
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice-platform"\npassword = "pw"\n'
            '[api]\ntimeout = 55\n',
        )
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        (project_dir / "config.toml").write_text('[api]\ntimeout = 123\n')

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.timeout == 123
        assert sources["timeout"] == SOURCE_PROJECT
        # username still from account layer
        assert cfg.username == "alice-platform"

    def test_accounts_section_in_account_config_is_ignored(
        self, home: Path, clean_env: None
    ) -> None:
        """Stray ``[accounts."<user>"]`` nesting inside a per-account file
        should NOT trigger the legacy catalog merge — one account = one file."""
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice-platform"\npassword = "pw"\n\n'
            '[accounts."ghost"]\npassword = "should-not-leak"\n',
        )

        cfg, _ = Config.from_files_and_env(require_credentials=True)

        # Credentials come from the flat [auth] section, not from [accounts.ghost].
        assert cfg.username == "alice-platform"
        assert cfg.password == "pw"
        # And the ignored section leaves no trace in config.accounts.
        assert cfg.accounts == {}

    def test_context_account_in_account_config_is_ignored(
        self, home: Path, clean_env: None
    ) -> None:
        """``[context].account`` has no effect inside a per-account file
        — the active account is already determined by ``~/.inspire/current``."""
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice-platform"\npassword = "pw"\n\n'
            '[context]\naccount = "bob"\n',
        )

        cfg, _ = Config.from_files_and_env(require_credentials=False)

        assert cfg.username == "alice-platform"
        assert cfg.context_account is None

    def test_writable_config_path_targets_active_account(
        self, home: Path, clean_env: None
    ) -> None:
        """``inspire init`` writes to the active account's config.toml so the
        data it saves is the same file the loader then reads."""
        self._write_account_config(home, "alice", '[auth]\nusername = "a"\n')

        target = Config.writable_config_path()
        assert target == home / ".inspire" / "accounts" / "alice" / "config.toml"

    def test_writable_config_path_falls_back_without_active_account(
        self, home: Path, clean_env: None
    ) -> None:
        target = Config.writable_config_path()
        assert target == Config.resolve_global_config_path()


# ===========================================================================
# Init command tests
# ===========================================================================


class TestInitCommand:
    """Tests for inspire init command."""

    @pytest.fixture
    def clean_env(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
        """Clear relevant env vars for testing."""
        # Clear all INSPIRE_* and INSP_* env vars
        for key in list(os.environ.keys()):
            if key.startswith("INSPIRE_") or key.startswith("INSP_"):
                monkeypatch.delenv(key, raising=False)
        yield

    def test_init_creates_template_when_no_env_vars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that init creates template config when no env vars detected."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        # Simulate choosing project config
        result = runner.invoke(init, input="p\n")

        assert result.exit_code == 0
        assert "No environment variables detected" in result.output
        config_file = tmp_path / ".inspire" / "config.toml"
        assert config_file.exists()
        content = config_file.read_text()
        assert "[auth]" in content
        assert "[api]" in content
        assert "your_username" in content  # Template placeholder

    def test_init_template_flag_creates_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that --template flag creates template even with env vars."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")

        runner = CliRunner()
        result = runner.invoke(init, ["--template", "--project"])

        assert result.exit_code == 0
        assert "Creating template config" in result.output
        config_file = tmp_path / ".inspire" / "config.toml"
        assert config_file.exists()
        content = config_file.read_text()
        # Should have template placeholder, not actual env var value
        assert "your_username" in content
        assert "testuser" not in content

    def test_init_json_template_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that init supports command-local --json output."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        result = runner.invoke(init, ["--json", "--template", "--project", "--force"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["success"] is True
        assert payload["data"]["mode"] == "template"
        assert payload["data"]["files_written"] == [str(tmp_path / ".inspire" / "config.toml")]
        assert payload["data"]["detected_env_count"] == 0
        assert payload["data"]["secret_env_count"] == 0

    def test_init_json_fails_when_overwrite_prompt_would_be_needed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that JSON mode fails fast instead of entering interactive overwrite prompts."""
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".inspire"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("[auth]\nusername = 'existing'")

        runner = CliRunner()
        result = runner.invoke(init, ["--json", "--template", "--project"])

        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["success"] is False
        assert payload["error"]["type"] == "ValidationError"
        assert "--force" in payload["error"]["message"]

    def test_init_help_includes_probe_pubkey_alias_and_scope_note(self) -> None:
        """Test probe option help text clearly states discover+probe scope."""
        runner = CliRunner()
        result = runner.invoke(init, ["--help"])

        assert result.exit_code == 0
        assert "Template/smart modes avoid writing secrets." in result.output
        assert "stored in global config for the selected account." in result.output
        assert "--probe-pubkey" in result.output
        assert "--pubkey" in result.output
        assert "Only effective with --discover" in result.output
        assert "shared-path" in result.output

    def test_init_global_creates_global_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that init --global creates global config."""
        global_config = tmp_path / ".config" / "inspire" / "config.toml"
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(init, ["--global", "--template"])

        assert result.exit_code == 0
        assert global_config.exists()
        content = global_config.read_text()
        assert "[auth]" in content

    def test_init_warns_on_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that init warns when config exists."""
        monkeypatch.chdir(tmp_path)

        # Create existing config
        config_dir = tmp_path / ".inspire"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text("[auth]\nusername = 'existing'")

        runner = CliRunner()
        # Simulate choosing 'p' then declining overwrite
        result = runner.invoke(init, input="p\nn\n")

        assert "already exists" in result.output
        assert "Aborted" in result.output
        # Original should be unchanged
        assert "existing" in config_file.read_text()

    def test_init_force_overwrites_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that --force overwrites existing config without prompting."""
        monkeypatch.chdir(tmp_path)

        # Create existing config
        config_dir = tmp_path / ".inspire"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text("[auth]\nusername = 'existing'")

        runner = CliRunner()
        result = runner.invoke(init, ["--template", "--project", "--force"])

        assert result.exit_code == 0
        content = config_file.read_text()
        assert "existing" not in content
        assert "your_username" in content

    def test_init_with_env_vars_auto_split(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test init with env vars uses auto-split by scope."""
        global_config = tmp_path / ".config" / "inspire" / "config.toml"
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        # Set both global and project scope env vars
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://custom.example.com")  # global
        monkeypatch.setenv("INSPIRE_TARGET_DIR", "/shared/myproject")  # project

        runner = CliRunner()
        result = runner.invoke(init, ["--force"])

        assert result.exit_code == 0

        # Both files should exist
        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        assert global_config.exists(), "Global config should be created"
        assert project_config.exists(), "Project config should be created"

        # Global config should have base_url only
        global_content = global_config.read_text()
        assert 'base_url = "https://custom.example.com"' in global_content
        assert "target_dir" not in global_content

        # Project config should have target_dir only
        project_content = project_config.read_text()
        assert 'target_dir = "/shared/myproject"' in project_content
        assert "base_url" not in project_content

    def test_init_global_flag_forces_all_to_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that --global forces all options to global config."""
        global_config = tmp_path / ".config" / "inspire" / "config.toml"
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        # Set both global and project scope env vars
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")  # global
        monkeypatch.setenv("INSPIRE_TARGET_DIR", "/shared/myproject")  # project

        runner = CliRunner()
        result = runner.invoke(init, ["--global", "--force"])

        assert result.exit_code == 0
        assert global_config.exists()

        # Global config should have BOTH values
        global_content = global_config.read_text()
        assert 'username = "testuser"' in global_content
        assert 'target_dir = "/shared/myproject"' in global_content

        # Project config should NOT exist
        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        assert not project_config.exists()

    def test_init_project_flag_forces_all_to_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that --project forces all options to project config."""
        monkeypatch.chdir(tmp_path)

        # Set both global and project scope env vars
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")  # global
        monkeypatch.setenv("INSPIRE_TARGET_DIR", "/shared/myproject")  # project

        runner = CliRunner()
        result = runner.invoke(init, ["--project", "--force"])

        assert result.exit_code == 0

        # Project config should have BOTH values
        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        assert project_config.exists()
        project_content = project_config.read_text()
        assert 'username = "testuser"' in project_content
        assert 'target_dir = "/shared/myproject"' in project_content

    def test_init_excludes_secrets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that init excludes secrets from config files."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "secretpass")

        runner = CliRunner()
        result = runner.invoke(init, ["--project", "--force"])

        assert result.exit_code == 0
        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        content = project_config.read_text()

        # Username should be written
        assert 'username = "testuser"' in content
        # Password should be excluded (commented)
        assert "secretpass" not in content
        assert "# password - use env var INSPIRE_PASSWORD for security" in content

    def test_init_both_flags_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that --global and --project together is an error."""
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(init, ["--global", "--project"])

        assert result.exit_code != 0
        assert "Cannot specify both" in result.output

    def test_init_auto_split_only_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test auto-split with only global-scope env vars."""
        global_config = tmp_path / ".config" / "inspire" / "config.toml"
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        # Set only global scope env vars
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://custom.example.com")
        monkeypatch.setenv("INSPIRE_TIMEOUT", "60")

        runner = CliRunner()
        result = runner.invoke(init, ["--force"])

        assert result.exit_code == 0

        # Global config should exist
        assert global_config.exists()
        global_content = global_config.read_text()
        assert 'base_url = "https://custom.example.com"' in global_content
        assert "timeout = 60" in global_content

        # Project config should NOT exist (no project-scope vars)
        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        assert not project_config.exists()

    def test_init_auto_split_only_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test auto-split with only project-scope env vars."""
        global_config = tmp_path / ".config" / "inspire" / "config.toml"
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        # Set only project scope env vars
        monkeypatch.setenv("INSPIRE_TARGET_DIR", "/shared/myproject")
        monkeypatch.setenv("INSP_GITHUB_REPO", "user/repo")

        runner = CliRunner()
        result = runner.invoke(init, ["--force"])

        assert result.exit_code == 0

        # Project config should exist
        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        assert project_config.exists()
        project_content = project_config.read_text()
        assert 'target_dir = "/shared/myproject"' in project_content
        assert 'repo = "user/repo"' in project_content

        # Global config should NOT exist (no global-scope vars)
        assert not global_config.exists()

    def test_init_discover_writes_per_account_catalog(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        global_config = tmp_path / ".config" / "inspire" / "config.toml"
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        workspace_id = "ws-11111111-1111-1111-1111-111111111111"
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://example.invalid")
        monkeypatch.setenv("INSPIRE_TARGET_DIR", "/shared/test")

        from inspire.platform.web.session.models import WebSession
        from inspire.platform.web.browser_api.availability.models import GPUAvailability
        from inspire.platform.web.browser_api.projects import ProjectInfo
        import inspire.platform.web.session as web_session_module
        import inspire.platform.web.browser_api as browser_api_module

        session = WebSession(
            storage_state={"cookies": [], "origins": []},
            created_at=0.0,
            workspace_id=workspace_id,
            login_username="testuser",
        )
        monkeypatch.setattr(web_session_module, "get_web_session", lambda **_: session)

        projects = [
            ProjectInfo(
                project_id="project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                name="Over Quota",
                workspace_id=workspace_id,
                member_gpu_limit=True,
                member_remain_gpu_hours=-1,
            ),
            ProjectInfo(
                project_id="project-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                name="Good Project",
                workspace_id=workspace_id,
                member_gpu_limit=True,
                member_remain_gpu_hours=10,
            ),
        ]
        monkeypatch.setattr(browser_api_module, "list_projects", lambda **_: projects)

        raw_groups = [
            {
                "logic_compute_group_id": "lcg-cccccccc-cccc-cccc-cccc-cccccccccccc",
                "name": "H100 (CUDA 12.8)",
            }
        ]
        monkeypatch.setattr(browser_api_module, "list_compute_groups", lambda **_: raw_groups)

        availability = [
            GPUAvailability(
                group_id="lcg-cccccccc-cccc-cccc-cccc-cccccccccccc",
                group_name="H100 (CUDA 12.8)",
                gpu_type="H100",
                total_gpus=8,
                used_gpus=0,
                available_gpus=8,
                low_priority_gpus=0,
            )
        ]
        monkeypatch.setattr(
            browser_api_module,
            "get_accurate_gpu_availability",
            lambda **_: availability,
        )

        monkeypatch.setattr(
            browser_api_module,
            "get_train_job_workdir",
            lambda *, project_id, workspace_id, session=None: f"/inspire/hdd/project/{project_id}",
        )

        runner = CliRunner()
        result = runner.invoke(init, ["--discover", "--force"])

        assert result.exit_code == 0

        assert global_config.exists()
        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        assert project_config.exists()

        global_data = Config._load_toml(global_config)
        assert global_data["api"]["base_url"] == "https://example.invalid"
        assert "workspaces" not in global_data
        assert "compute_groups" not in global_data
        assert "accounts" not in global_data

        project_data = Config._load_toml(project_config)
        assert project_data["context"]["account"] == "testuser"
        # Defaults to the best in-quota project
        assert project_data["context"]["project"] == "good-project"
        assert project_data["context"]["workspace_cpu"] == "cpu"
        assert project_data["context"]["workspace_gpu"] == "gpu"
        assert project_data["context"]["workspace_internet"] == "internet"
        assert project_data["workspaces"]["cpu"] == workspace_id
        assert project_data["workspaces"]["gpu"] == workspace_id
        assert project_data["workspaces"]["internet"] == workspace_id
        assert (
            project_data["projects"]["over-quota"] == "project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        )
        assert (
            project_data["projects"]["good-project"]
            == "project-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        )
        assert project_data["compute_groups"][0]["id"] == "lcg-cccccccc-cccc-cccc-cccc-cccccccccccc"
        assert project_data["compute_groups"][0]["gpu_type"] == "H100"
        account = project_data["accounts"]["testuser"]
        assert "password" not in account
        assert (
            account["project_catalog"]["project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]["name"]
            == "Over Quota"
        )
        assert (
            account["project_catalog"]["project-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"]["name"]
            == "Good Project"
        )

    def test_prompt_workspace_aliases_force_removes_duplicate_legacy_keys(self) -> None:
        from inspire.cli.commands.init.discover import _prompt_workspace_aliases

        merged_workspaces = {
            "cpu": "ws-cpu",
            "gpu": "ws-gpu",
            "internet": "ws-net",
            "hpc": "ws-hpc",
            "whole_node": "ws-node",
            "custom": "ws-custom",
        }

        _prompt_workspace_aliases(
            force=True,
            workspace_id="ws-cpu",
            merged_workspaces=merged_workspaces,
            env_overrides={},
            discovered_workspace_ids=[
                "ws-cpu",
                "ws-gpu",
                "ws-net",
                "ws-hpc",
                "ws-node",
            ],
            discovered_workspace_names={
                "ws-cpu": "CPU资源空间",
                "ws-gpu": "分布式训练空间",
                "ws-net": "上网空间",
                "ws-hpc": "高性能计算空间",
                "ws-node": "整节点空间",
            },
        )

        assert merged_workspaces["CPU资源空间"] == "ws-cpu"
        assert merged_workspaces["分布式训练空间"] == "ws-gpu"
        assert merged_workspaces["上网空间"] == "ws-net"
        assert merged_workspaces["高性能计算空间"] == "ws-hpc"
        assert merged_workspaces["整节点空间"] == "ws-node"
        assert merged_workspaces["custom"] == "ws-custom"
        for alias in ("cpu", "gpu", "internet", "hpc", "whole_node"):
            assert alias not in merged_workspaces

    def test_init_discover_force_preserves_existing_target_dir_and_cleans_obsolete_sections(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        from inspire.platform.web.session.models import WebSession
        from inspire.platform.web.browser_api.availability.models import GPUAvailability
        import inspire.platform.web.session as web_session_module
        import inspire.platform.web.browser_api as browser_api_module

        global_config, workspace_id = self._setup_discover_mocks(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://example.invalid")

        existing_target_dir = "/inspire/hdd/project/chj_code/video-reason"
        catalog_workdir = "/inspire/hdd/project/tongjingqi-CZXS25110029"
        gpu_workspace_id = "ws-22222222-2222-2222-2222-222222222222"
        internet_workspace_id = "ws-33333333-3333-3333-3333-333333333333"
        hpc_workspace_id = "ws-44444444-4444-4444-4444-444444444444"
        whole_node_workspace_id = "ws-55555555-5555-5555-5555-555555555555"

        session = WebSession(
            storage_state={"cookies": [], "origins": []},
            created_at=0.0,
            workspace_id=workspace_id,
            login_username="testuser",
            all_workspace_ids=[
                workspace_id,
                gpu_workspace_id,
                internet_workspace_id,
                hpc_workspace_id,
                whole_node_workspace_id,
            ],
            all_workspace_names={
                workspace_id: "CPU资源空间",
                gpu_workspace_id: "分布式训练空间",
                internet_workspace_id: "上网空间",
                hpc_workspace_id: "高性能计算空间",
                whole_node_workspace_id: "整节点空间",
            },
        )
        monkeypatch.setattr(web_session_module, "get_web_session", lambda **_: session)
        monkeypatch.setattr(web_session_module, "login_with_playwright", lambda *a, **kw: session)

        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        project_config.parent.mkdir(parents=True, exist_ok=True)
        project_config.write_text(
            (
                f"[paths]\n"
                f'target_dir = "{existing_target_dir}"\n\n'
                "[defaults]\n"
                'project = "old-project"\n\n'
                "[job]\n"
                'image = "job-image"\n\n'
                "[notebook]\n"
                'image = "notebook-image"\n\n'
                "[workspaces]\n"
                f'cpu = "{workspace_id}"\n'
                f'gpu = "{gpu_workspace_id}"\n'
                f'internet = "{internet_workspace_id}"\n'
                f'hpc = "{hpc_workspace_id}"\n'
                f'whole_node = "{whole_node_workspace_id}"\n'
            )
        )

        group_ids = {
            workspace_id: "cg-cpu",
            gpu_workspace_id: "cg-gpu",
            internet_workspace_id: "cg-internet",
            hpc_workspace_id: "cg-hpc",
            whole_node_workspace_id: "cg-whole-node",
        }
        group_names = {
            workspace_id: "CPU Group",
            gpu_workspace_id: "H100 (CUDA 12.8)",
            internet_workspace_id: "Internet CPU",
            hpc_workspace_id: "HPC H100",
            whole_node_workspace_id: "Whole Node H100",
        }
        gpu_types = {
            workspace_id: "CPU",
            gpu_workspace_id: "H100",
            internet_workspace_id: "CPU",
            hpc_workspace_id: "H100",
            whole_node_workspace_id: "H100",
        }

        def fake_list_compute_groups(*, workspace_id, **_):
            return [
                {
                    "logic_compute_group_id": group_ids[workspace_id],
                    "name": group_names[workspace_id],
                }
            ]

        def fake_get_accurate_gpu_availability(*, workspace_id, **_):
            gpu_type = gpu_types[workspace_id]
            total_gpus = 8 if gpu_type != "CPU" else 0
            return [
                GPUAvailability(
                    group_id=group_ids[workspace_id],
                    group_name=group_names[workspace_id],
                    gpu_type=gpu_type,
                    total_gpus=total_gpus,
                    used_gpus=0,
                    available_gpus=total_gpus,
                    low_priority_gpus=0,
                )
            ]

        monkeypatch.setattr(browser_api_module, "list_compute_groups", fake_list_compute_groups)
        monkeypatch.setattr(
            browser_api_module,
            "get_accurate_gpu_availability",
            fake_get_accurate_gpu_availability,
        )
        monkeypatch.setattr(
            browser_api_module,
            "get_train_job_workdir",
            lambda **_: catalog_workdir,
        )

        runner = CliRunner()
        result = runner.invoke(init, ["--discover", "--force"])

        assert result.exit_code == 0

        project_data = Config._load_toml(project_config)
        assert project_data["paths"]["target_dir"] == existing_target_dir
        assert "defaults" not in project_data
        assert "job" not in project_data
        assert "notebook" not in project_data
        assert project_data["workspaces"]["CPU资源空间"] == workspace_id
        assert project_data["workspaces"]["分布式训练空间"] == gpu_workspace_id
        assert project_data["workspaces"]["上网空间"] == internet_workspace_id
        assert project_data["workspaces"]["高性能计算空间"] == hpc_workspace_id
        assert project_data["workspaces"]["整节点空间"] == whole_node_workspace_id
        for alias in ("cpu", "gpu", "internet", "hpc", "whole_node"):
            assert alias not in project_data["workspaces"]

    def test_init_discover_collects_projects_across_discovered_workspaces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        global_config = tmp_path / ".config" / "inspire" / "config.toml"
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        ws_main = "ws-11111111-1111-1111-1111-111111111111"
        ws_extra = "ws-22222222-2222-2222-2222-222222222222"
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://example.invalid")
        monkeypatch.setenv("INSPIRE_TARGET_DIR", "/shared/test")

        from inspire.platform.web.session.models import WebSession
        from inspire.platform.web.browser_api.projects import ProjectInfo
        import inspire.platform.web.session as web_session_module
        import inspire.platform.web.browser_api as browser_api_module
        import inspire.platform.web.browser_api.workspaces as workspaces_module

        session = WebSession(
            storage_state={"cookies": [], "origins": []},
            created_at=0.0,
            workspace_id=ws_main,
            login_username="testuser",
            all_workspace_ids=[ws_main, ws_extra],
        )
        monkeypatch.setattr(web_session_module, "get_web_session", lambda **_: session)
        monkeypatch.setattr(workspaces_module, "try_enumerate_workspaces", lambda *_a, **_k: [])

        projects_by_workspace = {
            ws_main: [
                ProjectInfo(
                    project_id="project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    name="Main Project",
                    workspace_id=ws_main,
                )
            ],
            ws_extra: [
                ProjectInfo(
                    project_id="project-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    name="Extra Project",
                    workspace_id=ws_extra,
                )
            ],
        }
        list_calls: list[str] = []

        def fake_list_projects(*, workspace_id=None, session=None):  # type: ignore[no-untyped-def]
            ws = str(workspace_id or "").strip()
            if ws:
                list_calls.append(ws)
            return projects_by_workspace.get(ws, [])

        monkeypatch.setattr(browser_api_module, "list_projects", fake_list_projects)
        monkeypatch.setattr(browser_api_module, "list_compute_groups", lambda **_: [])
        monkeypatch.setattr(browser_api_module, "get_accurate_gpu_availability", lambda **_: [])
        monkeypatch.setattr(
            browser_api_module,
            "get_train_job_workdir",
            lambda *, project_id, workspace_id, session=None: f"/inspire/hdd/project/{project_id}",
        )

        runner = CliRunner()
        result = runner.invoke(init, ["--discover", "--force"])

        assert result.exit_code == 0
        assert sorted(list_calls) == [ws_main, ws_extra]

        global_data = Config._load_toml(global_config)
        account = global_data["accounts"]["testuser"]
        discovered_ids = set(account["projects"].values())
        assert discovered_ids == {
            "project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "project-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        }
        project_catalog = account["project_catalog"]
        assert "project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in project_catalog
        assert "project-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" in project_catalog

    # ------------------------------------------------------------------
    # Helper to set up discover mocks shared across credential tests
    # ------------------------------------------------------------------
    def _setup_discover_mocks(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        get_web_session_side_effect=None,
        login_session=None,
    ):
        """Wire up standard discover mocks and return (global_config, workspace_id)."""
        from inspire.platform.web.session.models import WebSession
        from inspire.platform.web.browser_api.availability.models import GPUAvailability
        from inspire.platform.web.browser_api.projects import ProjectInfo
        import inspire.platform.web.session as web_session_module
        import inspire.platform.web.browser_api as browser_api_module

        global_config = tmp_path / ".config" / "inspire" / "config.toml"
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        workspace_id = "ws-11111111-1111-1111-1111-111111111111"

        # Default session used by the fast path
        default_session = WebSession(
            storage_state={"cookies": [], "origins": []},
            created_at=0.0,
            workspace_id=workspace_id,
            login_username="cached-user",
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
            ),
        ]
        monkeypatch.setattr(browser_api_module, "list_projects", lambda **_: projects)
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
        monkeypatch.setattr(
            browser_api_module,
            "get_train_job_workdir",
            lambda **_: "/inspire/hdd/project/p1",
        )

        # Stub out _ensure_playwright_browser and _ensure_ssh_key so they never
        # touch the real filesystem or try to launch a browser.
        from inspire.cli.commands.init import discover as discover_module

        monkeypatch.setattr(discover_module, "_ensure_playwright_browser", lambda: None)
        monkeypatch.setattr(discover_module, "_ensure_ssh_key", lambda: None)

        return global_config, workspace_id

    def test_discover_cli_username_overrides_cached_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Finding 1: --username must override cached session's login_username."""
        from inspire.platform.web.session.models import WebSession

        workspace_id = "ws-11111111-1111-1111-1111-111111111111"
        cli_session = WebSession(
            storage_state={"cookies": [], "origins": []},
            created_at=0.0,
            workspace_id=workspace_id,
            login_username="cli-user",
        )

        monkeypatch.setenv("INSPIRE_BASE_URL", "https://example.invalid")

        global_config, _ = self._setup_discover_mocks(
            monkeypatch,
            tmp_path,
            # Fast path returns a session for "cached-user", but cli_username
            # should force the interactive path and ignore it.
            login_session=cli_session,
        )

        runner = CliRunner()
        result = runner.invoke(
            init,
            ["--discover", "--force", "--username", "cli-user"],
            input="secret-password\n",
        )

        assert result.exit_code == 0
        assert "Account: cli-user" in result.output

        global_data = Config._load_toml(global_config)
        assert "cli-user" in global_data.get("accounts", {})

    def test_discover_fallback_always_prompts_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Finding 2: password must always be prompted in the fallback path."""
        from inspire.platform.web.session.models import WebSession

        workspace_id = "ws-11111111-1111-1111-1111-111111111111"
        login_session = WebSession(
            storage_state={"cookies": [], "origins": []},
            created_at=0.0,
            workspace_id=workspace_id,
            login_username="newuser",
        )

        # Even with INSPIRE_PASSWORD set, the interactive fallback should
        # prompt again because the existing session failed.
        monkeypatch.setenv("INSPIRE_PASSWORD", "old-stale-pw")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://example.invalid")

        global_config, _ = self._setup_discover_mocks(
            monkeypatch,
            tmp_path,
            get_web_session_side_effect=ValueError("Missing credentials"),
            login_session=login_session,
        )

        runner = CliRunner()
        # Input provides: username, then password
        result = runner.invoke(
            init,
            ["--discover", "--force"],
            input="newuser\nfresh-password\n",
        )

        assert result.exit_code == 0
        assert "Note: prompted account password was stored in global config" in result.output
        # Verify the freshly prompted password (not the stale one) was persisted
        global_data = Config._load_toml(global_config)
        account = global_data["accounts"]["newuser"]
        assert account["password"] == "fresh-password"

    def test_discover_prompted_credentials_overwrite_stale_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Finding 3: prompted credentials must overwrite existing config values."""
        from inspire.platform.web.session.models import WebSession

        workspace_id = "ws-11111111-1111-1111-1111-111111111111"
        login_session = WebSession(
            storage_state={"cookies": [], "origins": []},
            created_at=0.0,
            workspace_id=workspace_id,
            login_username="testuser",
        )

        monkeypatch.setenv("INSPIRE_BASE_URL", "https://old-url.invalid")

        global_config, _ = self._setup_discover_mocks(
            monkeypatch,
            tmp_path,
            get_web_session_side_effect=ValueError("Missing credentials"),
            login_session=login_session,
        )

        # Pre-populate global config with stale values
        global_config.parent.mkdir(parents=True, exist_ok=True)
        global_config.write_text(
            '[api]\nbase_url = "https://old-url.invalid"\n\n'
            '[accounts.testuser]\npassword = "old-password"\n'
        )

        runner = CliRunner()
        result = runner.invoke(
            init,
            ["--discover", "--force", "--base-url", "https://new-url.invalid"],
            input="testuser\nnew-password\n",
        )

        assert result.exit_code == 0
        global_data = Config._load_toml(global_config)
        assert global_data["api"]["base_url"] == "https://new-url.invalid"
        assert global_data["accounts"]["testuser"]["password"] == "new-password"

    def test_discover_probe_respects_limit_and_forwards_probe_flags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        from inspire.platform.web.browser_api.projects import ProjectInfo
        import inspire.config.ssh_runtime as ssh_runtime_module
        import inspire.platform.web.browser_api as browser_api_module
        from inspire.cli.commands.init import discover as discover_module

        global_config, workspace_id = self._setup_discover_mocks(monkeypatch, tmp_path)
        monkeypatch.setenv("INSPIRE_USERNAME", "probe-user")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://example.invalid")

        projects = [
            ProjectInfo(
                project_id="project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                name="Alpha",
                workspace_id=workspace_id,
            ),
            ProjectInfo(
                project_id="project-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                name="Beta",
                workspace_id=workspace_id,
            ),
            ProjectInfo(
                project_id="project-cccccccc-cccc-cccc-cccc-cccccccccccc",
                name="Gamma",
                workspace_id=workspace_id,
            ),
        ]
        monkeypatch.setattr(browser_api_module, "list_projects", lambda **_: projects)
        monkeypatch.setattr(browser_api_module, "get_train_job_workdir", lambda **_: "")

        monkeypatch.setattr(
            discover_module, "_load_ssh_public_key", lambda _path: "ssh-ed25519 AAA"
        )
        monkeypatch.setattr(
            discover_module,
            "_select_probe_cpu_compute_group_id",
            lambda _compute_groups: "lcg-cpu",
        )
        monkeypatch.setattr(
            discover_module,
            "_select_probe_cpu_quota",
            lambda _schedule: ("quota-cpu", 4, 32),
        )
        monkeypatch.setattr(
            discover_module,
            "_select_probe_image",
            lambda _images: SimpleNamespace(image_id="img-1", url="docker://img-1"),
        )
        monkeypatch.setattr(browser_api_module, "list_notebook_compute_groups", lambda **_: [])
        monkeypatch.setattr(browser_api_module, "get_notebook_schedule", lambda **_: {})
        monkeypatch.setattr(browser_api_module, "list_images", lambda **_: [])
        monkeypatch.setattr(
            ssh_runtime_module,
            "resolve_ssh_runtime_config",
            lambda: SimpleNamespace(),
        )

        probe_calls: list[dict] = []

        def fake_probe(**kwargs):
            probe_calls.append(kwargs)
            return {"shared_path_group": f"/inspire/hdd/global_user/{kwargs['project_alias']}"}

        monkeypatch.setattr(discover_module, "_probe_project_shared_path_group", fake_probe)

        runner = CliRunner()
        result = runner.invoke(
            init,
            [
                "--discover",
                "--force",
                "--probe-shared-path",
                "--probe-limit",
                "2",
                "--probe-keep-notebooks",
                "--probe-timeout",
                "111",
                "--probe-pubkey",
                "/tmp/key.pub",
            ],
        )

        assert result.exit_code == 0
        assert len(probe_calls) == 2
        assert all(call["keep_notebook"] is True for call in probe_calls)
        assert all(call["timeout"] == 111 for call in probe_calls)
        assert all(call["account_key"] == "probe-user" for call in probe_calls)

        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        project_data = Config._load_toml(project_config)
        project_catalog = project_data["accounts"]["probe-user"]["project_catalog"]
        assert (
            project_catalog["project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]["shared_path_group"]
            == "/inspire/hdd/global_user/alpha"
        )
        assert (
            project_catalog["project-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"]["shared_path_group"]
            == "/inspire/hdd/global_user/beta"
        )
        assert project_catalog["project-cccccccc-cccc-cccc-cccc-cccccccccccc"].get(
            "shared_path_group"
        ) in ("", None)

    def test_discover_probe_keeps_successful_updates_on_partial_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        from inspire.platform.web.browser_api.projects import ProjectInfo
        import inspire.config.ssh_runtime as ssh_runtime_module
        import inspire.platform.web.browser_api as browser_api_module
        from inspire.cli.commands.init import discover as discover_module

        global_config, workspace_id = self._setup_discover_mocks(monkeypatch, tmp_path)
        monkeypatch.setenv("INSPIRE_USERNAME", "probe-user")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://example.invalid")

        projects = [
            ProjectInfo(
                project_id="project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                name="Alpha",
                workspace_id=workspace_id,
            ),
            ProjectInfo(
                project_id="project-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                name="Beta",
                workspace_id=workspace_id,
            ),
        ]
        monkeypatch.setattr(browser_api_module, "list_projects", lambda **_: projects)
        monkeypatch.setattr(browser_api_module, "get_train_job_workdir", lambda **_: "")

        monkeypatch.setattr(
            discover_module, "_load_ssh_public_key", lambda _path: "ssh-ed25519 AAA"
        )
        monkeypatch.setattr(
            discover_module,
            "_select_probe_cpu_compute_group_id",
            lambda _compute_groups: "lcg-cpu",
        )
        monkeypatch.setattr(
            discover_module,
            "_select_probe_cpu_quota",
            lambda _schedule: ("quota-cpu", 4, 32),
        )
        monkeypatch.setattr(
            discover_module,
            "_select_probe_image",
            lambda _images: SimpleNamespace(image_id="img-1", url="docker://img-1"),
        )
        monkeypatch.setattr(browser_api_module, "list_notebook_compute_groups", lambda **_: [])
        monkeypatch.setattr(browser_api_module, "get_notebook_schedule", lambda **_: {})
        monkeypatch.setattr(browser_api_module, "list_images", lambda **_: [])
        monkeypatch.setattr(
            ssh_runtime_module,
            "resolve_ssh_runtime_config",
            lambda: SimpleNamespace(),
        )

        def fake_probe(**kwargs):
            if kwargs["project_alias"] == "alpha":
                return {"shared_path_group": "/inspire/hdd/global_user/alpha"}
            return {"shared_path_group": "", "probe_error": "probe failed"}

        monkeypatch.setattr(discover_module, "_probe_project_shared_path_group", fake_probe)

        runner = CliRunner()
        result = runner.invoke(init, ["--discover", "--force", "--probe-shared-path"])

        assert result.exit_code == 0

        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        project_data = Config._load_toml(project_config)
        project_catalog = project_data["accounts"]["probe-user"]["project_catalog"]
        assert (
            project_catalog["project-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]["shared_path_group"]
            == "/inspire/hdd/global_user/alpha"
        )
        assert project_catalog["project-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"].get(
            "shared_path_group"
        ) in ("", None)

    def test_discover_probe_fails_when_probe_defaults_cannot_be_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        import inspire.platform.web.browser_api as browser_api_module
        from inspire.cli.commands.init import discover as discover_module

        self._setup_discover_mocks(monkeypatch, tmp_path)
        monkeypatch.setenv("INSPIRE_USERNAME", "probe-user")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://example.invalid")

        monkeypatch.setattr(
            discover_module, "_load_ssh_public_key", lambda _path: "ssh-ed25519 AAA"
        )
        monkeypatch.setattr(browser_api_module, "list_notebook_compute_groups", lambda **_: [])
        monkeypatch.setattr(
            discover_module,
            "_select_probe_cpu_compute_group_id",
            lambda _compute_groups: None,
        )

        runner = CliRunner()
        result = runner.invoke(init, ["--discover", "--force", "--probe-shared-path"])

        assert result.exit_code == 1
        assert "Failed to resolve probe defaults" in result.output


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
        monkeypatch.setenv("INSPIRE_TIMEOUT", "60")

        detected = _detect_env_vars()
        toml_content = _generate_toml_content(detected)

        assert "[auth]" in toml_content
        assert 'username = "testuser"' in toml_content
        assert "[api]" in toml_content
        assert 'base_url = "https://custom.example.com"' in toml_content
        assert "timeout = 60" in toml_content

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
        monkeypatch.setenv("INSPIRE_TARGET_DIR", "/shared/myproject")  # project

        detected = _detect_env_vars()

        # Generate with global filter
        global_content = _generate_toml_content(detected, scope_filter="global")
        assert 'base_url = "https://custom.example.com"' in global_content
        assert "target_dir" not in global_content

        # Generate with project filter
        project_content = _generate_toml_content(detected, scope_filter="project")
        assert "base_url" not in project_content
        assert 'target_dir = "/shared/myproject"' in project_content

        # Generate without filter (all options)
        all_content = _generate_toml_content(detected)
        assert 'base_url = "https://custom.example.com"' in all_content
        assert 'target_dir = "/shared/myproject"' in all_content

    def test_generate_toml_list_values(
        self, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test TOML generation with list values."""
        monkeypatch.setenv("INSPIRE_BRIDGE_DENYLIST", "*.pyc,__pycache__,*.log")

        detected = _detect_env_vars()
        toml_content = _generate_toml_content(detected)

        assert "[bridge]" in toml_content
        assert 'denylist = ["*.pyc", "__pycache__", "*.log"]' in toml_content

    def test_generate_toml_preserves_special_chars(
        self, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that special characters in values are properly escaped."""
        monkeypatch.setenv("INSPIRE_BASE_URL", 'https://example.com/path?foo=bar&baz="test"')

        detected = _detect_env_vars()
        toml_content = _generate_toml_content(detected)

        # Value should be properly escaped
        assert 'base_url = "https://example.com/path?foo=bar&baz=\\"test\\""' in toml_content

    def test_derive_shared_path_group_extracts_global_user_dir(self) -> None:
        group = _derive_shared_path_group(
            "/inspire/hdd/global_user/user123/some/dir",
            account_key=None,
        )
        assert group == "/inspire/hdd/global_user/user123"

    def test_derive_shared_path_group_infers_global_user_dir_without_account_match(self) -> None:
        group = _derive_shared_path_group(
            "/inspire/hdd/project/myproj/user-dir",
            account_key="acct-0000",
        )
        assert group == "/inspire/hdd/global_user/user-dir"

    def test_derive_shared_path_group_falls_back_to_project_root_when_user_dir_missing(
        self,
    ) -> None:
        group = _derive_shared_path_group(
            "/inspire/hdd/project/myproj",
            account_key="acct-0000",
        )
        assert group == "/inspire/hdd/project/myproj"

    def test_derive_shared_path_group_normalizes_global_user_under_project_path(self) -> None:
        group = _derive_shared_path_group(
            "/inspire/hdd/project/myproj/global_user/user-dir",
            account_key=None,
        )
        assert group == "/inspire/hdd/global_user/user-dir"


# ===========================================================================
# Config show command tests
# ===========================================================================


class TestConfigShowCommand:
    """Tests for inspire config show command."""

    def test_config_show_table(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test config show table output."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(config_command, ["show"])

        assert result.exit_code == 0
        assert "Configuration Overview" in result.output
        assert "INSPIRE_USERNAME" in result.output
        assert "testuser" in result.output
        assert "[env]" in result.output

    def test_config_show_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test config show JSON output."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(config_command, ["show", "--format", "json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "config_files" in data
        assert "values" in data
        assert "INSPIRE_USERNAME" in data["values"]

    def test_config_show_json_alias(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test config show supports --json alias."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(config_command, ["show", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "config_files" in data
        assert "values" in data
        assert "INSPIRE_USERNAME" in data["values"]

    def test_config_show_filter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test config show with category filter."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(config_command, ["show", "--filter", "auth"])

        assert result.exit_code == 0
        assert "Authentication" in result.output
        # Other categories should not appear
        assert "GitHub" not in result.output


# ===========================================================================
# Config env command tests
# ===========================================================================


class TestConfigEnvCommand:
    """Tests for inspire config env command."""

    def test_config_env_minimal(self) -> None:
        """Test config env minimal template."""
        runner = CliRunner()
        result = runner.invoke(config_command, ["env"])

        assert result.exit_code == 0
        assert "# Inspire CLI Environment Variables" in result.output
        assert "INSPIRE_USERNAME" in result.output
        # Minimal should include essential categories
        assert "=== Authentication ===" in result.output
        assert "=== API ===" in result.output

    def test_config_env_full(self) -> None:
        """Test config env full template."""
        runner = CliRunner()
        result = runner.invoke(config_command, ["env", "--template", "full"])

        assert result.exit_code == 0
        # Full template should include all categories
        assert "=== Job ===" in result.output
        assert "=== Notebook ===" in result.output

    def test_config_env_output_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test config env writing to file."""
        monkeypatch.chdir(tmp_path)
        output_file = tmp_path / ".env.example"

        runner = CliRunner()
        result = runner.invoke(config_command, ["env", "--output", str(output_file)])

        assert result.exit_code == 0
        assert output_file.exists()
        content = output_file.read_text()
        assert "INSPIRE_USERNAME" in content


# ===========================================================================
# Migrate command removed - verify it no longer exists
# ===========================================================================


class TestMigrateCommandRemoved:
    """Tests to verify migrate command has been removed."""

    def test_migrate_command_does_not_exist(self) -> None:
        """Test that 'inspire config migrate' is no longer a valid command."""
        runner = CliRunner()
        result = runner.invoke(config_command, ["migrate"])

        # Should fail with "No such command"
        assert result.exit_code != 0
        assert "No such command" in result.output or "Error" in result.output


# ===========================================================================
# prefer_source tests
# ===========================================================================


class TestPreferSource:
    """Tests for the [cli] prefer_source config setting."""

    @pytest.fixture
    def clean_env(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
        """Clear relevant env vars for testing."""
        env_vars = [
            "INSPIRE_USERNAME",
            "INSPIRE_PASSWORD",
            "INSPIRE_BASE_URL",
            "INSPIRE_TIMEOUT",
            "INSPIRE_REQUESTS_HTTP_PROXY",
            "INSPIRE_REQUESTS_HTTPS_PROXY",
            "INSPIRE_PLAYWRIGHT_PROXY",
            "INSPIRE_RTUNNEL_PROXY",
            "INSPIRE_TARGET_DIR",
            "INSP_GITHUB_SERVER",
        ]
        for var in env_vars:
            monkeypatch.delenv(var, raising=False)
        yield

    def test_default_env_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that env vars override project TOML by default (no prefer_source)."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[api]
timeout = 120
"""
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_TIMEOUT", "90")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.timeout == 90
        assert sources["timeout"] == SOURCE_ENV
        assert cfg.prefer_source == "env"

    def test_prefer_source_env_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that prefer_source = 'env' lets env vars win (same as default)."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[cli]
prefer_source = "env"

[api]
timeout = 120
"""
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_TIMEOUT", "90")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.timeout == 90
        assert sources["timeout"] == SOURCE_ENV

    def test_prefer_source_toml_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that prefer_source = 'toml' keeps project TOML values over env vars."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[cli]
prefer_source = "toml"

[api]
timeout = 120
"""
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_TIMEOUT", "90")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.timeout == 120
        assert sources["timeout"] == SOURCE_PROJECT
        assert cfg.prefer_source == "toml"

    def test_prefer_source_toml_env_fills_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that prefer_source = 'toml' still picks up env vars for fields NOT in project TOML."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[cli]
prefer_source = "toml"

[api]
timeout = 120
"""
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)
        # Set env var for a field NOT in the project TOML
        monkeypatch.setenv("INSPIRE_USERNAME", "envuser")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        # timeout should stay from project TOML
        assert cfg.timeout == 120
        assert sources["timeout"] == SOURCE_PROJECT
        # username should come from env (not set in project TOML)
        assert cfg.username == "envuser"
        assert sources["username"] == SOURCE_ENV

    def test_prefer_source_toml_global_still_overridden_by_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that prefer_source = 'toml' only protects project TOML, not global TOML."""
        # Create global config
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text(
            """
[api]
timeout = 45
"""
        )

        # Create project config with prefer_source but NOT setting timeout
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[cli]
prefer_source = "toml"
"""
        )

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_TIMEOUT", "90")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        # timeout from global TOML should be overridden by env var
        assert cfg.timeout == 90
        assert sources["timeout"] == SOURCE_ENV

    def test_password_resolves_from_global_accounts_map(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Project username should pick password from global [accounts] mapping."""
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text(
            """
[accounts."toml-user"]
password = "global-pass"
"""
        )

        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[cli]
prefer_source = "toml"

[auth]
username = "toml-user"
"""
        )

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_PASSWORD", "env-pass")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.username == "toml-user"
        assert cfg.password == "global-pass"
        assert sources["password"] == SOURCE_GLOBAL

    def test_password_resolves_from_project_accounts_map(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Project config [accounts] password should be used for the selected user."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[auth]
username = "toml-user"

[accounts."toml-user"]
password = "project-pass"
"""
        )

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_PASSWORD", "env-pass")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.password == "project-pass"
        assert sources["password"] == SOURCE_PROJECT
        assert sources["accounts"] == SOURCE_PROJECT

    def test_password_project_accounts_override_global_accounts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Project [accounts] password should override global for the same username."""
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text(
            """
[accounts."toml-user"]
password = "global-pass"
"""
        )

        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[auth]
username = "toml-user"

[accounts."toml-user"]
password = "project-pass"
"""
        )

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_PASSWORD", "env-pass")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.password == "project-pass"
        assert sources["password"] == SOURCE_PROJECT
        assert sources["accounts"] == SOURCE_PROJECT

    def test_password_accounts_override_auth_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Account password should take precedence over legacy [auth].password."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[auth]
username = "toml-user"
password = "auth-pass"

[accounts."toml-user"]
password = "account-pass"
"""
        )

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_PASSWORD", "env-pass")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.password == "account-pass"
        assert sources["password"] == SOURCE_PROJECT

    def test_project_account_catalog_merges_with_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Project account metadata should merge with global and take precedence on conflicts."""
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text(
            """
[accounts."toml-user"]
shared_path_group = "global-shared"
train_job_workdir = "/global/workdir"

[accounts."toml-user".projects]
common = "project-global-common"
global_only = "project-global-only"

[accounts."toml-user".workspaces]
cpu = "ws-global-cpu"
gpu = "ws-global-gpu"

[accounts."toml-user".api]
timeout = 70

[[accounts."toml-user".compute_groups]]
name = "Global CG"
id = "lcg-global"
gpu_type = "H100"

[accounts."toml-user".project_catalog."project-global-common"]
shared_path_group = "group-global"
workdir = "/global/common"
"""
        )

        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[context]
account = "toml-user"

[accounts."toml-user"]
password = "project-pass"
shared_path_group = "project-shared"
train_job_workdir = "/project/workdir"

[accounts."toml-user".projects]
common = "project-project-common"
project_only = "project-project-only"

[accounts."toml-user".workspaces]
gpu = "ws-project-gpu"
internet = "ws-project-internet"

[accounts."toml-user".api]
timeout = 99

[[accounts."toml-user".compute_groups]]
name = "Project CG"
id = "lcg-project"
gpu_type = "H200"

[accounts."toml-user".project_catalog."project-global-common"]
shared_path_group = "group-project"

[accounts."toml-user".project_catalog."project-project-only"]
workdir = "/project/only"
"""
        )

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_PASSWORD", "env-pass")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.password == "project-pass"
        assert cfg.timeout == 99
        assert cfg.projects["common"] == "project-project-common"
        assert cfg.projects["global_only"] == "project-global-only"
        assert cfg.projects["project_only"] == "project-project-only"
        assert cfg.workspaces["cpu"] == "ws-global-cpu"
        assert cfg.workspaces["gpu"] == "ws-project-gpu"
        assert cfg.workspaces["internet"] == "ws-project-internet"
        assert cfg.compute_groups[0]["id"] == "lcg-project"
        assert cfg.account_shared_path_group == "project-shared"
        assert cfg.account_train_job_workdir == "/project/workdir"
        assert cfg.project_shared_path_groups["project-global-common"] == "group-project"
        assert cfg.project_workdirs["project-project-only"] == "/project/only"
        assert sources["accounts"] == SOURCE_PROJECT
        assert sources["password"] == SOURCE_PROJECT
        assert sources["project_catalog"] == SOURCE_PROJECT
        assert sources["project_shared_path_groups"] == SOURCE_PROJECT
        assert sources["project_workdirs"] == SOURCE_PROJECT
        assert sources["account_shared_path_group"] == SOURCE_PROJECT
        assert sources["account_train_job_workdir"] == SOURCE_PROJECT

    def test_password_env_used_when_global_account_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """INSPIRE_PASSWORD should still work when no global account password is defined."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[auth]
username = "toml-user"
"""
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_PASSWORD", "env-pass")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.password == "env-pass"
        assert sources["password"] == SOURCE_ENV

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
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
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
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(config_command, ["show"])

        assert result.exit_code == 0
        assert "Precedence:" in result.output
        assert "project TOML wins" in result.output

    def test_config_show_displays_default_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that config show displays default precedence when no prefer_source set."""
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(config_command, ["show"])

        assert result.exit_code == 0
        assert "Precedence:" in result.output
        assert "env vars win" in result.output

    def test_config_show_json_includes_prefer_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that config show --format json includes prefer_source."""
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text(
            """
[cli]
prefer_source = "toml"
"""
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(config_command, ["show", "--format", "json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["prefer_source"] == "toml"
