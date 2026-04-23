"""Tests for TOML config file loading and layered configuration."""

import json
import os
import re
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
        # Isolate from a real ~/.inspire/current on the dev machine.
        fake_home = tmp_path / "__home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.base_url == "https://api.example.com"
        assert cfg.timeout == 30
        assert sources["base_url"] == SOURCE_DEFAULT
        assert sources["timeout"] == SOURCE_DEFAULT

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
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.username == "projectuser"
        assert cfg.timeout == 120
        assert sources["username"] == SOURCE_PROJECT
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

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_USERNAME", "envuser")
        monkeypatch.setenv("INSPIRE_TIMEOUT", "90")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        # Env vars should override
        assert cfg.username == "envuser"
        assert cfg.timeout == 90
        assert sources["username"] == SOURCE_ENV
        assert sources["timeout"] == SOURCE_ENV

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
        # config.accounts no longer exists as a dataclass field — the legacy
        # catalog is gone entirely.
        assert not hasattr(cfg, "accounts")

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
        # [context] is stripped at the account layer; no side effect.
        assert not hasattr(cfg, "context_account")

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
            ('[paths]\ntarget_dir = "/inspire/ssd/foo"', "paths.target_dir"),
            ('[paths]\nlog_pattern = "train_*.log"', "paths.log_pattern"),
            ('[github]\nrepo = "me/foo"', "github.repo"),
            ('[job]\nproject_id = "project-abc"', "job.project_id"),
            ('[job]\nworkspace_id = "ws-xyz"', "job.workspace_id"),
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

    def test_empty_paths_section_in_account_config_is_tolerated(
        self, home: Path, clean_env: None
    ) -> None:
        """Empty [paths] (e.g. left over from a template) shouldn't blow up."""
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice"\npassword = "pw"\n\n[paths]\n',
        )

        cfg, _ = Config.from_files_and_env(require_credentials=False)
        assert cfg.username == "alice"

    def test_allowed_account_defaults_still_work(
        self, home: Path, clean_env: None
    ) -> None:
        """Fields that CAN be account-level defaults must keep working
        (workspace aliases, default image/priority, etc.)."""
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice"\npassword = "pw"\n\n'
            '[workspaces]\ncpu = "ws-cpu"\ngpu = "ws-gpu"\n\n'
            '[job]\npriority = 5\nimage = "myimage:latest"\n\n'
            '[notebook]\nresource = "1xH100"\n',
        )

        cfg, _ = Config.from_files_and_env(require_credentials=False)
        assert cfg.workspace_cpu_id == "ws-cpu"
        assert cfg.workspace_gpu_id == "ws-gpu"
        assert cfg.job_priority == 5
        assert cfg.job_image == "myimage:latest"
        assert cfg.notebook_resource == "1xH100"

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
        assert sources["requests_https_proxy"] == SOURCE_GLOBAL

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
        assert sources["remote_env"] == SOURCE_GLOBAL

    def test_remote_env_project_merges_with_account(
        self, home: Path, clean_env: None, tmp_path: Path
    ) -> None:
        self._write_account_config(
            home,
            "alice",
            '[auth]\nusername = "alice"\npassword = "pw"\n\n'
            '[remote_env]\nWANDB_API_KEY = "account-key"\nUV_PYTHON_INSTALL_DIR = "/opt/uv"\n',
        )
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        (project_dir / "config.toml").write_text(
            '[remote_env]\nWANDB_API_KEY = "project-key"\nHF_TOKEN = "hf"\n'
        )

        cfg, sources = Config.from_files_and_env(require_credentials=False)
        assert cfg.remote_env == {
            "WANDB_API_KEY": "project-key",
            "UV_PYTHON_INSTALL_DIR": "/opt/uv",
            "HF_TOKEN": "hf",
        }
        assert sources["remote_env"] == SOURCE_PROJECT

    def test_require_credentials_without_active_account_raises(
        self, home: Path, clean_env: None
    ) -> None:
        with pytest.raises(ConfigError, match="No active Inspire account"):
            Config.from_files_and_env(require_credentials=True)

    def test_get_config_paths_returns_account_and_project(
        self, home: Path, clean_env: None, tmp_path: Path
    ) -> None:
        self._write_account_config(
            home, "alice", '[auth]\nusername = "alice"\n'
        )
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text('[api]\ntimeout = 77\n')

        account_path, proj_path = Config.get_config_paths()
        assert account_path == home / ".inspire" / "accounts" / "alice" / "config.toml"
        assert proj_path == project_config


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

    def test_init_auto_split_only_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test auto-split with only project-scope env vars."""
        global_config = tmp_path / ".config" / "inspire" / "config.toml"
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

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_TIMEOUT", "90")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        # timeout from global TOML should be overridden by env var
        assert cfg.timeout == 90
        assert sources["timeout"] == SOURCE_ENV

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
        result = runner.invoke(config_command, ["show"])

        assert result.exit_code == 0
        assert "Precedence:" in result.output
        assert "project TOML wins" in result.output

    def test_config_show_displays_default_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that config show displays default precedence when no prefer_source set."""
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
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(config_command, ["show", "--format", "json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["prefer_source"] == "toml"
