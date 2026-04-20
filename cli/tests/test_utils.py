"""Tests for utility modules: job_cache, config, tunnel."""

from pathlib import Path

import pytest

from inspire.cli.utils.job_cache import JobCache
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
    generate_ssh_config,
    load_tunnel_config,
    save_tunnel_config,
    _get_proxy_command,
    has_internet_for_gpu_type,
)

# ===========================================================================
# JobCache tests
# ===========================================================================


class TestJobCache:
    """Tests for JobCache class."""

    def test_add_and_get_job(self, tmp_path: Path) -> None:
        """Test adding and retrieving a job."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        cache.add_job(
            job_id="job-12345678-1234-1234-1234-123456789abc",
            name="test-job",
            resource="4xH200",
            command="python train.py",
            status="RUNNING",
            log_path="/train/logs/test.log",
        )

        job = cache.get_job("job-12345678-1234-1234-1234-123456789abc")
        assert job is not None
        assert job["job_id"] == "job-12345678-1234-1234-1234-123456789abc"
        assert job["name"] == "test-job"
        assert job["resource"] == "4xH200"
        assert job["command"] == "python train.py"
        assert job["status"] == "RUNNING"
        assert job["log_path"] == "/train/logs/test.log"

    def test_get_nonexistent_job(self, tmp_path: Path) -> None:
        """Test getting a job that doesn't exist."""
        cache = JobCache(str(tmp_path / "jobs.json"))
        job = cache.get_job("job-nonexistent-0000-0000-000000000000")
        assert job is None

    def test_update_status(self, tmp_path: Path) -> None:
        """Test updating job status."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        cache.add_job(
            job_id="job-12345678-1234-1234-1234-123456789abc",
            name="test-job",
            resource="H200",
            command="echo test",
            status="PENDING",
        )

        cache.update_status("job-12345678-1234-1234-1234-123456789abc", "RUNNING")

        job = cache.get_job("job-12345678-1234-1234-1234-123456789abc")
        assert job is not None
        assert job["status"] == "RUNNING"

    def test_list_jobs_sorted_by_creation(self, tmp_path: Path) -> None:
        """Test that jobs are sorted by creation time (newest first)."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        cache.add_job(
            job_id="job-aaaaaaa1-0000-0000-0000-000000000001",
            name="job-1",
            resource="H200",
            command="echo 1",
            status="RUNNING",
        )
        cache.add_job(
            job_id="job-aaaaaaa2-0000-0000-0000-000000000002",
            name="job-2",
            resource="H200",
            command="echo 2",
            status="PENDING",
        )

        jobs = cache.list_jobs(limit=10)
        assert len(jobs) == 2
        # Most recent should be first
        assert jobs[0]["name"] == "job-2"
        assert jobs[1]["name"] == "job-1"

    def test_list_jobs_with_status_filter(self, tmp_path: Path) -> None:
        """Test filtering jobs by status."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        cache.add_job(
            job_id="job-aaaaaaa1-0000-0000-0000-000000000001",
            name="running-job",
            resource="H200",
            command="echo 1",
            status="RUNNING",
        )
        cache.add_job(
            job_id="job-aaaaaaa2-0000-0000-0000-000000000002",
            name="pending-job",
            resource="H200",
            command="echo 2",
            status="PENDING",
        )

        running_jobs = cache.list_jobs(status="RUNNING")
        assert len(running_jobs) == 1
        assert running_jobs[0]["name"] == "running-job"

    def test_list_jobs_with_exclude_statuses(self, tmp_path: Path) -> None:
        """Test excluding jobs by status."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        cache.add_job(
            job_id="job-aaaaaaa1-0000-0000-0000-000000000001",
            name="running-job",
            resource="H200",
            command="echo 1",
            status="RUNNING",
        )
        cache.add_job(
            job_id="job-aaaaaaa2-0000-0000-0000-000000000002",
            name="failed-job",
            resource="H200",
            command="echo 2",
            status="FAILED",
        )

        active_jobs = cache.list_jobs(exclude_statuses={"FAILED", "CANCELLED"})
        assert len(active_jobs) == 1
        assert active_jobs[0]["name"] == "running-job"

    def test_list_jobs_with_limit(self, tmp_path: Path) -> None:
        """Test limiting number of returned jobs."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        for i in range(5):
            cache.add_job(
                job_id=f"job-aaaaaaa{i}-0000-0000-0000-00000000000{i}",
                name=f"job-{i}",
                resource="H200",
                command=f"echo {i}",
                status="RUNNING",
            )

        jobs = cache.list_jobs(limit=3)
        assert len(jobs) == 3

    def test_remove_job(self, tmp_path: Path) -> None:
        """Test removing a job from cache."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        cache.add_job(
            job_id="job-12345678-1234-1234-1234-123456789abc",
            name="test-job",
            resource="H200",
            command="echo test",
            status="RUNNING",
        )

        assert cache.remove_job("job-12345678-1234-1234-1234-123456789abc") is True
        assert cache.get_job("job-12345678-1234-1234-1234-123456789abc") is None

        # Removing nonexistent job returns False
        assert cache.remove_job("job-nonexistent-0000-0000-000000000000") is False

    def test_clear_cache(self, tmp_path: Path) -> None:
        """Test clearing all jobs from cache."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        cache.add_job(
            job_id="job-12345678-1234-1234-1234-123456789abc",
            name="test-job",
            resource="H200",
            command="echo test",
            status="RUNNING",
        )

        cache.clear()

        jobs = cache.list_jobs()
        assert len(jobs) == 0

    def test_log_offset_operations(self, tmp_path: Path) -> None:
        """Test log offset get/set/reset operations."""
        cache = JobCache(str(tmp_path / "jobs.json"))
        job_id = "job-12345678-1234-1234-1234-123456789abc"

        cache.add_job(
            job_id=job_id,
            name="test-job",
            resource="H200",
            command="echo test",
            status="RUNNING",
        )

        # Initial offset should be 0
        assert cache.get_log_offset(job_id) == 0

        # Set offset
        cache.set_log_offset(job_id, 1000)
        assert cache.get_log_offset(job_id) == 1000

        # Reset offset
        cache.reset_log_offset(job_id)
        assert cache.get_log_offset(job_id) == 0

    def test_default_cache_path(self) -> None:
        """Test that default cache path is in home directory."""
        cache = JobCache()
        expected_path = Path.home() / ".inspire" / "jobs.json"
        assert cache.cache_path == expected_path


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
        """Test error when username is missing."""
        monkeypatch.delenv("INSPIRE_USERNAME", raising=False)
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")

        with pytest.raises(ConfigError, match="Missing INSPIRE_USERNAME"):
            Config.from_env()

    def test_from_env_missing_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test error when password is missing."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.delenv("INSPIRE_PASSWORD", raising=False)

        with pytest.raises(ConfigError, match="Missing INSPIRE_PASSWORD"):
            Config.from_env()

    def test_from_env_require_target_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test error when target dir is required but missing."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.delenv("INSPIRE_TARGET_DIR", raising=False)

        with pytest.raises(ConfigError, match="Missing INSPIRE_TARGET_DIR"):
            Config.from_env(require_target_dir=True)

    def test_from_env_with_target_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading config with target dir."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setenv("INSPIRE_TARGET_DIR", "/shared/train")

        config = Config.from_env(require_target_dir=True)

        assert config.target_dir == "/shared/train"

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

    def test_from_env_invalid_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test error with invalid timeout value."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setenv("INSPIRE_TIMEOUT", "not-a-number")

        with pytest.raises(ConfigError, match="Invalid INSPIRE_TIMEOUT"):
            Config.from_env()

    def test_get_expanded_cache_path(self) -> None:
        """Test that cache path ~ is expanded."""
        config = Config(
            username="test",
            password="test",
            job_cache_path="~/.inspire/jobs.json",
        )

        expanded = config.get_expanded_cache_path()
        assert "~" not in expanded
        assert ".inspire/jobs.json" in expanded


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

    def test_save_and_load(self, tmp_path: Path) -> None:
        """Test saving and loading tunnel config."""
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

    def test_load_tunnel_config_prefers_resolved_username(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "bridges-canonical-user.json").write_text(
            """
{
  "default": "canonical",
  "bridges": [
    {"name": "canonical", "proxy_url": "https://canonical.example.com"}
  ]
}
""".strip()
        )
        (tmp_path / "bridges-primary.json").write_text(
            """
{
  "default": "legacy",
  "bridges": [
    {"name": "legacy", "proxy_url": "https://legacy.example.com"}
  ]
}
""".strip()
        )

        monkeypatch.setenv("INSPIRE_ACCOUNT", "primary")
        monkeypatch.setattr(
            Config,
            "from_files_and_env",
            classmethod(
                lambda cls, require_target_dir=False, require_credentials=True: (
                    Config(username="canonical-user", password=""),
                    {},
                )
            ),
        )

        loaded = load_tunnel_config(tmp_path)

        assert loaded.account == "canonical-user"
        assert "canonical" in loaded.bridges
        assert loaded.default_bridge == "canonical"

    def test_load_tunnel_config_merges_account_alias_and_legacy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "bridges-canonical-user.json").write_text(
            """
{
  "default": "canonical",
  "bridges": [
    {"name": "canonical", "proxy_url": "https://canonical.example.com"},
    {"name": "shared", "proxy_url": "https://primary-wins.example.com"}
  ]
}
""".strip()
        )
        (tmp_path / "bridges-primary.json").write_text(
            """
{
  "default": "legacy-alias",
  "bridges": [
    {"name": "shared", "proxy_url": "https://alias-should-not-win.example.com"},
    {"name": "alias-only", "proxy_url": "https://alias-only.example.com"}
  ]
}
""".strip()
        )
        (tmp_path / "bridges.json").write_text(
            """
{
  "default": "legacy",
  "bridges": [
    {"name": "legacy-only", "proxy_url": "https://legacy-only.example.com"}
  ]
}
""".strip()
        )

        monkeypatch.setenv("INSPIRE_ACCOUNT", "primary")
        monkeypatch.setattr(
            Config,
            "from_files_and_env",
            classmethod(
                lambda cls, require_target_dir=False, require_credentials=True: (
                    Config(username="canonical-user", password=""),
                    {},
                )
            ),
        )

        loaded = load_tunnel_config(tmp_path)

        assert loaded.default_bridge == "canonical"
        assert "canonical" in loaded.bridges
        assert "alias-only" in loaded.bridges
        assert "legacy-only" in loaded.bridges
        assert loaded.bridges["shared"].proxy_url == "https://primary-wins.example.com"

    def test_load_tunnel_config_falls_back_to_env_username_on_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "bridges-env-user.json").write_text(
            """
{
  "default": "env-bridge",
  "bridges": [
    {"name": "env-bridge", "proxy_url": "https://env.example.com"}
  ]
}
""".strip()
        )
        (tmp_path / "bridges.json").write_text(
            """
{
  "default": "legacy",
  "bridges": [
    {"name": "legacy", "proxy_url": "https://legacy.example.com"}
  ]
}
""".strip()
        )

        monkeypatch.setenv("INSPIRE_USERNAME", "env-user")
        monkeypatch.setattr(
            Config,
            "from_files_and_env",
            classmethod(
                lambda cls, require_target_dir=False, require_credentials=True: (
                    _ for _ in ()
                ).throw(ConfigError("broken config"))
            ),
        )

        loaded = load_tunnel_config(tmp_path)

        assert loaded.account == "env-user"
        assert loaded.default_bridge == "env-bridge"
        assert "env-bridge" in loaded.bridges
        assert "legacy" in loaded.bridges


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

    def test_generate_ssh_config_reuses_shell_quoted_proxy_command(self, tmp_path: Path) -> None:
        """Generated ssh-config should preserve safe shell quoting for ProxyCommand."""
        bridge = BridgeProfile(
            name="test",
            proxy_url="https://proxy.example.com/tunnel?token=a*b",
        )
        rtunnel_bin = tmp_path / "rtunnel with space"

        ssh_config = generate_ssh_config(bridge, rtunnel_bin, host_alias="mybridge")
        expected_proxy = _get_proxy_command(bridge, rtunnel_bin, quiet=False)

        assert "Host mybridge" in ssh_config
        assert f"ProxyCommand {expected_proxy}" in ssh_config

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
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://qz.sii.edu.cn")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:7897")

        cmd = _get_proxy_command(bridge, rtunnel_bin, quiet=False)

        assert "HTTP_PROXY=http://127.0.0.1:7897" in cmd
