"""Tests for utility modules: config, tunnel."""

from pathlib import Path

import pytest

from inspire.config import (
    Config,
    ConfigError,
    _parse_denylist,
    _parse_remote_timeout,
    build_env_exports,
)
from inspire.bridge.tunnel import (
    BridgeProfile,
    TunnelConfig,
    load_tunnel_config,
    save_tunnel_config,
    _get_proxy_command,
    has_internet_for_gpu_type,
)


# ===========================================================================
# Config tests
# ===========================================================================


class TestConfig:
    """Tests for Config class and helper functions."""

    def test_from_env_with_required_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading config from environment variables."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.delenv("INSPIRE_BASE_URL", raising=False)

        config = Config.from_env()

        assert config.username == "testuser"
        assert config.password == "testpass"
        assert config.base_url == "https://api.example.com"

    def test_from_env_missing_username(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """v4.0.0 collapses identity-error wording into a single account-add prompt."""
        monkeypatch.delenv("INSPIRE_USERNAME", raising=False)
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")

        with pytest.raises(ConfigError, match="Missing platform credentials"):
            Config.from_env()

    def test_from_env_missing_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """v4.0.0 collapses identity-error wording into a single account-add prompt."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.delenv("INSPIRE_PASSWORD", raising=False)

        with pytest.raises(ConfigError, match="Missing platform credentials"):
            Config.from_env()

    def test_from_env_with_api_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading config with custom API settings."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setenv("INSPIRE_TIMEOUT", "60")
        monkeypatch.setenv("INSPIRE_MAX_RETRIES", "5")
        monkeypatch.setenv("INSPIRE_RETRY_DELAY", "2.5")

        config = Config.from_env()

        assert config.timeout == 60
        assert config.max_retries == 5
        assert config.retry_delay == 2.5

    def test_from_env_loads_job_notification_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setenv("INSPIRE_JOB_ENABLE_NOTIFICATION", "true")

        config = Config.from_env()

        assert config.job_enable_notification is True

    def test_from_env_invalid_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test error with invalid timeout value."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setenv("INSPIRE_TIMEOUT", "not-a-number")

        with pytest.raises(ConfigError, match="Invalid INSPIRE_TIMEOUT"):
            Config.from_env()


class TestConfigHelpers:
    """Tests for config helper functions."""

    def test_parse_remote_timeout_valid(self) -> None:
        """Test parsing valid timeout values."""
        assert _parse_remote_timeout("90") == 90
        assert _parse_remote_timeout("300") == 300
        assert _parse_remote_timeout("5") == 5

    def test_parse_remote_timeout_invalid(self) -> None:
        """Test parsing invalid timeout values."""
        with pytest.raises(ConfigError, match="Invalid INSP_REMOTE_TIMEOUT"):
            _parse_remote_timeout("not-a-number")

    def test_parse_denylist_empty(self) -> None:
        """Test parsing empty denylist."""
        assert _parse_denylist(None) == []
        assert _parse_denylist("") == []

    def test_parse_denylist_comma_separated(self) -> None:
        """Test parsing comma-separated denylist."""
        result = _parse_denylist("*.pyc, *.pyo, __pycache__")
        assert result == ["*.pyc", "*.pyo", "__pycache__"]

    def test_parse_denylist_newline_separated(self) -> None:
        """Test parsing newline-separated denylist."""
        result = _parse_denylist("*.pyc\n*.pyo\n__pycache__")
        assert result == ["*.pyc", "*.pyo", "__pycache__"]

    def test_parse_denylist_mixed(self) -> None:
        """Test parsing mixed separator denylist."""
        result = _parse_denylist("*.pyc, *.pyo\n__pycache__")
        assert result == ["*.pyc", "*.pyo", "__pycache__"]

    def test_build_env_exports_empty(self) -> None:
        """Test building env exports with empty dict."""
        assert build_env_exports({}) == ""

    def test_build_env_exports_single(self) -> None:
        """Test building env exports with single var."""
        result = build_env_exports({"FOO": "bar"})
        assert result == "export FOO=bar && "

    def test_build_env_exports_multiple(self) -> None:
        """Test building env exports with multiple vars."""
        result = build_env_exports({"FOO": "bar", "BAZ": "qux"})
        # Order may vary due to dict iteration, so check both parts
        assert "export FOO=bar" in result
        assert "export BAZ=qux" in result
        assert result.endswith(" && ")
        assert " && " in result  # Separates the two exports

    def test_build_env_exports_env_ref_bare(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """remote_env supports $VARNAME to pull from local environment."""
        monkeypatch.setenv("TOKEN", "sekret")
        result = build_env_exports({"WANDB_API_KEY": "$TOKEN"})
        assert result == "export WANDB_API_KEY=sekret && "

    def test_build_env_exports_env_ref_braced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """remote_env supports ${VARNAME} to pull from local environment."""
        monkeypatch.setenv("TOKEN", "sekret")
        result = build_env_exports({"WANDB_API_KEY": "${TOKEN}"})
        assert result == "export WANDB_API_KEY=sekret && "

    def test_build_env_exports_empty_uses_same_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty remote_env value uses the local environment value for that key."""
        monkeypatch.setenv("WANDB_API_KEY", "sekret")
        result = build_env_exports({"WANDB_API_KEY": ""})
        assert result == "export WANDB_API_KEY=sekret && "

    def test_build_env_exports_quotes_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Values are safely shell-quoted."""
        monkeypatch.setenv("TOKEN", "has spaces")
        result = build_env_exports({"WANDB_API_KEY": "$TOKEN"})
        assert result == "export WANDB_API_KEY='has spaces' && "

    def test_build_env_exports_missing_env_var_raises(self) -> None:
        """Missing env var references should fail early."""
        with pytest.raises(ConfigError, match="not set in the local environment"):
            build_env_exports({"WANDB_API_KEY": "$MISSING"})

    def test_build_env_exports_invalid_key_raises(self) -> None:
        """Invalid shell variable names should fail early."""
        with pytest.raises(ConfigError, match="Invalid remote_env key"):
            build_env_exports({"NOT-VALID": "x"})


# ===========================================================================
# Tunnel tests
# ===========================================================================


class TestHasInternetForGpuType:
    """Tests for has_internet_for_gpu_type helper function."""

    def test_empty_gpu_type_returns_true(self) -> None:
        """Empty GPU type defaults to having internet (CPU)."""
        assert has_internet_for_gpu_type("") is True

    def test_none_returns_true(self) -> None:
        """None GPU type defaults to having internet."""
        # Type hint says str, but handle None gracefully
        assert has_internet_for_gpu_type(None) is True  # type: ignore[arg-type]

    def test_h200_returns_false(self) -> None:
        """H200 GPUs don't have internet."""
        assert has_internet_for_gpu_type("H200") is False
        assert has_internet_for_gpu_type("h200") is False
        assert has_internet_for_gpu_type("H200-SXM") is False

    def test_h100_returns_false(self) -> None:
        """H100 GPUs don't have internet."""
        assert has_internet_for_gpu_type("H100") is False
        assert has_internet_for_gpu_type("h100") is False
        assert has_internet_for_gpu_type("H100-SXM") is False

    def test_4090_returns_true(self) -> None:
        """4090 GPUs have internet."""
        assert has_internet_for_gpu_type("4090") is True
        assert has_internet_for_gpu_type("RTX 4090") is True

    def test_cpu_returns_true(self) -> None:
        """CPU (no GPU) has internet."""
        assert has_internet_for_gpu_type("CPU") is True


class TestBridgeProfile:
    """Tests for BridgeProfile dataclass."""

    def test_to_dict(self) -> None:
        """Test converting profile to dict."""
        profile = BridgeProfile(
            name="test-bridge",
            proxy_url="https://proxy.example.com",
            ssh_user="admin",
            ssh_port=22222,
            rtunnel_port=31337,
        )

        d = profile.to_dict()

        assert d["name"] == "test-bridge"
        assert d["proxy_url"] == "https://proxy.example.com"
        assert d["ssh_user"] == "admin"
        assert d["ssh_port"] == 22222
        assert d["rtunnel_port"] == 31337

    def test_from_dict(self) -> None:
        """Test creating profile from dict."""
        d = {
            "name": "test-bridge",
            "proxy_url": "https://proxy.example.com",
            "ssh_user": "admin",
            "ssh_port": 22222,
            "rtunnel_port": 31337,
        }

        profile = BridgeProfile.from_dict(d)

        assert profile.name == "test-bridge"
        assert profile.proxy_url == "https://proxy.example.com"
        assert profile.ssh_user == "admin"
        assert profile.ssh_port == 22222
        assert profile.rtunnel_port == 31337

    def test_from_dict_with_defaults(self) -> None:
        """Test creating profile from dict with default values."""
        d = {
            "name": "test-bridge",
            "proxy_url": "https://proxy.example.com",
        }

        profile = BridgeProfile.from_dict(d)

        assert profile.name == "test-bridge"
        assert profile.ssh_user == "root"  # default
        assert profile.ssh_port == 22222  # default
        assert profile.has_internet is True  # default
        assert profile.rtunnel_port == 31337

    def test_from_dict_infers_rtunnel_port_from_proxy_url(self) -> None:
        d = {
            "name": "test-bridge",
            "proxy_url": "https://proxy.example.com/notebook/abc/proxy/32222/",
        }

        profile = BridgeProfile.from_dict(d)

        assert profile.rtunnel_port == 32222

    def test_has_internet_field(self) -> None:
        """Test has_internet field in BridgeProfile."""
        profile_with_internet = BridgeProfile(
            name="bridge1",
            proxy_url="https://proxy.example.com",
            has_internet=True,
        )
        profile_without_internet = BridgeProfile(
            name="bridge2",
            proxy_url="https://proxy.example.com",
            has_internet=False,
        )

        # Test to_dict includes has_internet
        assert profile_with_internet.to_dict()["has_internet"] is True
        assert profile_without_internet.to_dict()["has_internet"] is False

        # Test from_dict with has_internet
        d = {
            "name": "test",
            "proxy_url": "https://proxy.example.com",
            "has_internet": False,
        }
        profile = BridgeProfile.from_dict(d)
        assert profile.has_internet is False

        # Test backward compatibility - missing has_internet defaults to True
        d_legacy = {
            "name": "legacy",
            "proxy_url": "https://proxy.example.com",
        }
        profile_legacy = BridgeProfile.from_dict(d_legacy)
        assert profile_legacy.has_internet is True


class TestTunnelConfig:
    """Tests for TunnelConfig class."""

    def test_add_bridge(self) -> None:
        """Test adding a bridge profile."""
        config = TunnelConfig()
        profile = BridgeProfile(
            name="test-bridge",
            proxy_url="https://proxy.example.com",
        )

        config.add_bridge(profile)

        assert "test-bridge" in config.bridges
        assert config.default_bridge == "test-bridge"

    def test_get_bridge_by_name(self) -> None:
        """Test getting bridge by name."""
        config = TunnelConfig()
        profile = BridgeProfile(
            name="test-bridge",
            proxy_url="https://proxy.example.com",
        )
        config.add_bridge(profile)

        retrieved = config.get_bridge("test-bridge")

        assert retrieved is not None
        assert retrieved.name == "test-bridge"

    def test_get_default_bridge(self) -> None:
        """Test getting default bridge."""
        config = TunnelConfig()
        profile = BridgeProfile(
            name="my-bridge",
            proxy_url="https://proxy.example.com",
        )
        config.add_bridge(profile)

        retrieved = config.get_bridge()  # No name = get default

        assert retrieved is not None
        assert retrieved.name == "my-bridge"

    def test_remove_bridge(self) -> None:
        """Test removing a bridge."""
        config = TunnelConfig()
        profile = BridgeProfile(
            name="test-bridge",
            proxy_url="https://proxy.example.com",
        )
        config.add_bridge(profile)

        result = config.remove_bridge("test-bridge")

        assert result is True
        assert "test-bridge" not in config.bridges
        assert config.default_bridge is None

    def test_list_bridges(self) -> None:
        """Test listing all bridges."""
        config = TunnelConfig()
        profile1 = BridgeProfile(name="bridge1", proxy_url="https://p1.example.com")
        profile2 = BridgeProfile(name="bridge2", proxy_url="https://p2.example.com")
        config.add_bridge(profile1)
        config.add_bridge(profile2)

        bridges = config.list_bridges()

        assert len(bridges) == 2
        names = {b.name for b in bridges}
        assert names == {"bridge1", "bridge2"}

    def test_get_bridge_with_internet_prefers_default(self) -> None:
        """Test get_bridge_with_internet prefers the default bridge."""
        config = TunnelConfig()
        # Add bridge1 as default (first added)
        config.add_bridge(
            BridgeProfile(name="bridge1", proxy_url="https://p1.example.com", has_internet=True)
        )
        config.add_bridge(
            BridgeProfile(name="bridge2", proxy_url="https://p2.example.com", has_internet=True)
        )

        result = config.get_bridge_with_internet()

        assert result is not None
        assert result.name == "bridge1"  # Default bridge

    def test_get_bridge_with_internet_skips_no_internet_default(self) -> None:
        """Test get_bridge_with_internet skips default if it has no internet."""
        config = TunnelConfig()
        config.add_bridge(
            BridgeProfile(
                name="gpu-bridge", proxy_url="https://gpu.example.com", has_internet=False
            )
        )
        config.add_bridge(
            BridgeProfile(name="cpu-bridge", proxy_url="https://cpu.example.com", has_internet=True)
        )
        # gpu-bridge is default (first added)
        assert config.default_bridge == "gpu-bridge"

        result = config.get_bridge_with_internet()

        assert result is not None
        assert result.name == "cpu-bridge"  # Falls back to bridge with internet

    def test_get_bridge_with_internet_returns_none_when_all_no_internet(self) -> None:
        """Test get_bridge_with_internet returns None when no bridge has internet."""
        config = TunnelConfig()
        config.add_bridge(
            BridgeProfile(name="bridge1", proxy_url="https://p1.example.com", has_internet=False)
        )
        config.add_bridge(
            BridgeProfile(name="bridge2", proxy_url="https://p2.example.com", has_internet=False)
        )

        result = config.get_bridge_with_internet()

        assert result is None

    def test_get_bridge_with_internet_empty_config(self) -> None:
        """Test get_bridge_with_internet returns None for empty config."""
        config = TunnelConfig()

        result = config.get_bridge_with_internet()

        assert result is None


class TestTunnelConfigPersistence:
    """Tests for tunnel config save/load."""

    def test_save_and_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test saving and loading tunnel config."""
        # Isolate from any real ~/.inspire/current on the dev machine.
        import inspire.accounts as accounts_mod

        monkeypatch.setattr(accounts_mod, "current_account", lambda: None)

        config = TunnelConfig(config_dir=tmp_path)
        profile = BridgeProfile(
            name="test-bridge",
            proxy_url="https://proxy.example.com",
            ssh_user="testuser",
            ssh_port=12345,
        )
        config.add_bridge(profile)

        save_tunnel_config(config)

        loaded = load_tunnel_config(tmp_path)

        assert "test-bridge" in loaded.bridges
        assert loaded.default_bridge == "test-bridge"
        bridge = loaded.bridges["test-bridge"]
        assert bridge.proxy_url == "https://proxy.example.com"
        assert bridge.ssh_user == "testuser"
        assert bridge.ssh_port == 12345

    def test_account_scoped_save_lands_under_accounts_dir(
        self, tmp_path: Path
    ) -> None:
        config = TunnelConfig(config_dir=tmp_path, account="alice")
        config.add_bridge(
            BridgeProfile(name="b1", proxy_url="https://p.example.com")
        )
        save_tunnel_config(config)

        assert (tmp_path / "accounts" / "alice" / "bridges.json").exists()
        # No legacy-style sibling file.
        assert not (tmp_path / "bridges-alice.json").exists()

    def test_explicit_account_param_overrides_current_pointer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import inspire.accounts as accounts_mod

        # ``current_account()`` would pick ``bob``; explicit param says ``alice``.
        monkeypatch.setattr(accounts_mod, "current_account", lambda: "bob")

        (tmp_path / "accounts" / "alice").mkdir(parents=True)
        (tmp_path / "accounts" / "alice" / "bridges.json").write_text(
            '{"default": "a", "bridges": [{"name": "a", "proxy_url": "https://a.example.com"}]}'
        )
        (tmp_path / "accounts" / "bob").mkdir(parents=True)
        (tmp_path / "accounts" / "bob" / "bridges.json").write_text(
            '{"default": "b", "bridges": [{"name": "b", "proxy_url": "https://b.example.com"}]}'
        )

        loaded = load_tunnel_config(tmp_path, account="alice")
        assert loaded.account == "alice"
        assert "a" in loaded.bridges
        assert "b" not in loaded.bridges

    def test_current_pointer_used_when_no_explicit_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import inspire.accounts as accounts_mod

        monkeypatch.setattr(accounts_mod, "current_account", lambda: "bob")

        (tmp_path / "accounts" / "bob").mkdir(parents=True)
        (tmp_path / "accounts" / "bob" / "bridges.json").write_text(
            '{"default": "b", "bridges": [{"name": "b", "proxy_url": "https://b.example.com"}]}'
        )

        loaded = load_tunnel_config(tmp_path)
        assert loaded.account == "bob"
        assert "b" in loaded.bridges

    def test_no_account_uses_unscoped_bridges_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import inspire.accounts as accounts_mod

        monkeypatch.setattr(accounts_mod, "current_account", lambda: None)

        (tmp_path / "bridges.json").write_text(
            '{"default": "u", "bridges": [{"name": "u", "proxy_url": "https://u.example.com"}]}'
        )

        loaded = load_tunnel_config(tmp_path)
        assert loaded.account is None
        assert "u" in loaded.bridges

    def test_env_var_chains_are_not_consulted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INSPIRE_ACCOUNT / INSPIRE_BRIDGE_ACCOUNT / INSPIRE_USERNAME used
        to flow into tunnel account resolution. They no longer do."""
        import inspire.accounts as accounts_mod

        monkeypatch.setattr(accounts_mod, "current_account", lambda: None)
        monkeypatch.setenv("INSPIRE_ACCOUNT", "ghost")
        monkeypatch.setenv("INSPIRE_BRIDGE_ACCOUNT", "ghost")
        monkeypatch.setenv("INSPIRE_USERNAME", "ghost")

        (tmp_path / "bridges-ghost.json").write_text(
            '{"default": "g", "bridges": [{"name": "g", "proxy_url": "https://g.example.com"}]}'
        )

        loaded = load_tunnel_config(tmp_path)
        # No account was resolved from the env vars, and the unscoped
        # bridges.json does not exist either — nothing should load.
        assert loaded.account is None
        assert loaded.bridges == {}


class TestProxyCommand:
    """Tests for SSH proxy command building."""

    def test_get_proxy_command_https_url(self, tmp_path: Path) -> None:
        """Test building proxy command from https URL."""
        bridge = BridgeProfile(
            name="test",
            proxy_url="https://proxy.example.com/tunnel",
        )
        rtunnel_bin = tmp_path / "rtunnel"

        cmd = _get_proxy_command(bridge, rtunnel_bin, quiet=False)

        # Should convert https to wss
        assert "wss://proxy.example.com/tunnel" in cmd
        assert str(rtunnel_bin) in cmd or "rtunnel" in cmd

    def test_get_proxy_command_with_quiet(self, tmp_path: Path) -> None:
        """Test building proxy command with quiet flag."""
        bridge = BridgeProfile(
            name="test",
            proxy_url="https://proxy.example.com/tunnel",
        )
        rtunnel_bin = tmp_path / "rtunnel"

        cmd = _get_proxy_command(bridge, rtunnel_bin, quiet=True)

        # Should include stderr redirect
        assert "2>/dev/null" in cmd

    def test_get_proxy_command_injects_rtunnel_proxy_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bridge = BridgeProfile(
            name="test",
            proxy_url="https://proxy.example.com/tunnel",
        )
        rtunnel_bin = tmp_path / "rtunnel"
        monkeypatch.setenv("INSPIRE_RTUNNEL_PROXY", "http://127.0.0.1:7897")

        cmd = _get_proxy_command(bridge, rtunnel_bin, quiet=False)

        assert "HTTP_PROXY=http://127.0.0.1:7897" in cmd
        assert "HTTPS_PROXY=http://127.0.0.1:7897" in cmd
        assert "wss://proxy.example.com/tunnel" in cmd

    def test_get_proxy_command_uses_rtunnel_proxy_from_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bridge = BridgeProfile(
            name="test",
            proxy_url="https://proxy.example.com/tunnel",
        )
        rtunnel_bin = tmp_path / "rtunnel"
        cfg = Config(
            username="",
            password="",
            base_url="https://qz.sii.edu.cn",
            rtunnel_proxy="http://127.0.0.1:7897",
        )
        monkeypatch.setattr(
            Config,
            "from_files_and_env",
            classmethod(lambda cls, **kwargs: (cfg, {})),
        )

        cmd = _get_proxy_command(bridge, rtunnel_bin, quiet=False)

        assert "HTTP_PROXY=http://127.0.0.1:7897" in cmd
        assert "HTTPS_PROXY=http://127.0.0.1:7897" in cmd

    def test_get_proxy_command_reuses_qizhi_mixed_proxy(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bridge = BridgeProfile(
            name="test",
            proxy_url="https://proxy.example.com/tunnel",
        )
        rtunnel_bin = tmp_path / "rtunnel"
        monkeypatch.setattr(
            Config,
            "from_files_and_env",
            classmethod(lambda cls, **kwargs: (_ for _ in ()).throw(RuntimeError("no config"))),
        )
        for key in (
            "INSPIRE_RTUNNEL_PROXY",
            "inspire_rtunnel_proxy",
            "INSPIRE_PLAYWRIGHT_PROXY",
            "inspire_playwright_proxy",
            "PLAYWRIGHT_PROXY",
            "INSPIRE_REQUESTS_HTTP_PROXY",
            "INSPIRE_REQUESTS_HTTPS_PROXY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "https_proxy",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://qz.sii.edu.cn")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:7897")

        cmd = _get_proxy_command(bridge, rtunnel_bin, quiet=False)

        assert "HTTP_PROXY=http://127.0.0.1:7897" in cmd

    def test_exec_rtunnel_proxy_quiet_redirects_stderr_during_exec(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import inspire.bridge.tunnel.ssh as ssh_module

        class ExecCalled(Exception):
            pass

        bridge = BridgeProfile(
            name="test",
            proxy_url="https://proxy.example.com/proxy/31337/",
        )
        config = TunnelConfig()
        calls: list[tuple] = []

        def fake_execve(path, args, env):  # noqa: ANN001
            calls.append(("execve", path, args, env))
            raise ExecCalled()

        monkeypatch.setattr(ssh_module, "_ensure_rtunnel_binary", lambda _config: None)
        monkeypatch.setattr(ssh_module, "_proxy_env", lambda: {})
        monkeypatch.setattr(ssh_module.os, "dup", lambda fd: calls.append(("dup", fd)) or 99)
        monkeypatch.setattr(
            ssh_module.os,
            "open",
            lambda path, flags: calls.append(("open", path, flags)) or 88,
        )
        monkeypatch.setattr(
            ssh_module.os,
            "dup2",
            lambda src, dst: calls.append(("dup2", src, dst)),
        )
        monkeypatch.setattr(ssh_module.os, "close", lambda fd: calls.append(("close", fd)))
        monkeypatch.setattr(ssh_module.os, "execve", fake_execve)

        with pytest.raises(ExecCalled):
            ssh_module.exec_rtunnel_proxy(bridge, config, quiet=True)

        assert calls[0] == ("dup", 2)
        assert calls[1] == ("open", ssh_module.os.devnull, ssh_module.os.O_WRONLY)
        assert calls[2] == ("dup2", 88, 2)
        assert calls[3] == ("close", 88)
        assert calls[4][0] == "execve"
        assert calls[4][2][1] == "wss://proxy.example.com/proxy/31337/"
        assert calls[4][2][2] == "stdio://localhost:22222"
        assert calls[-2:] == [("dup2", 99, 2), ("close", 99)]
