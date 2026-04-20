"""Tests for SSH runtime config resolution and rtunnel URL usage."""

from __future__ import annotations

from pathlib import Path

import pytest

from inspire.bridge.tunnel.rtunnel import _get_rtunnel_download_url
from inspire.config import Config, ConfigError
from inspire.config.ssh_runtime import DEFAULT_RTUNNEL_DOWNLOAD_URL, resolve_ssh_runtime_config


def _write_project_config(root: Path, content: str) -> None:
    project_dir = root / ".inspire"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "config.toml").write_text(content)


class TestSshRuntimeConfig:
    @pytest.fixture
    def clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env_vars = [
            "INSPIRE_RTUNNEL_BIN",
            "INSPIRE_SSHD_DEB_DIR",
            "INSPIRE_DROPBEAR_DEB_DIR",
            "INSPIRE_SETUP_SCRIPT",
            "INSPIRE_RTUNNEL_DOWNLOAD_URL",
            "INSPIRE_RTUNNEL_UPLOAD_POLICY",
        ]
        for var in env_vars:
            monkeypatch.delenv(var, raising=False)

    def test_prefer_source_env_default_env_wins(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_env: None,
    ) -> None:
        _write_project_config(
            tmp_path,
            """
[ssh]
rtunnel_bin = "/project/rtunnel"
""",
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_RTUNNEL_BIN", "/env/rtunnel")

        runtime = resolve_ssh_runtime_config()

        assert runtime.rtunnel_bin == "/env/rtunnel"

    def test_prefer_source_toml_project_wins_over_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_env: None,
    ) -> None:
        _write_project_config(
            tmp_path,
            """
[cli]
prefer_source = "toml"

[ssh]
rtunnel_bin = "/project/rtunnel"
""",
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_RTUNNEL_BIN", "/env/rtunnel")

        runtime = resolve_ssh_runtime_config()

        assert runtime.rtunnel_bin == "/project/rtunnel"

    def test_prefer_source_toml_env_still_overrides_global(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_env: None,
    ) -> None:
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text(
            """
[ssh]
rtunnel_bin = "/global/rtunnel"
"""
        )

        _write_project_config(
            tmp_path,
            """
[cli]
prefer_source = "toml"
""",
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_RTUNNEL_BIN", "/env/rtunnel")

        runtime = resolve_ssh_runtime_config()

        assert runtime.rtunnel_bin == "/env/rtunnel"

    def test_cli_override_has_highest_priority(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_env: None,
    ) -> None:
        _write_project_config(
            tmp_path,
            """
[cli]
prefer_source = "toml"

[ssh]
rtunnel_bin = "/project/rtunnel"
""",
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_RTUNNEL_BIN", "/env/rtunnel")

        runtime = resolve_ssh_runtime_config(cli_overrides={"rtunnel_bin": "/cli/rtunnel"})

        assert runtime.rtunnel_bin == "/cli/rtunnel"

    def test_rtunnel_download_url_follows_prefer_source_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_env: None,
    ) -> None:
        _write_project_config(
            tmp_path,
            """
[cli]
prefer_source = "toml"

[ssh]
rtunnel_download_url = "https://project.example/rtunnel.tgz"
""",
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_RTUNNEL_DOWNLOAD_URL", "https://env.example/rtunnel.tgz")

        runtime = resolve_ssh_runtime_config()

        assert runtime.rtunnel_download_url == "https://project.example/rtunnel.tgz"

    def test_rtunnel_download_url_default_when_unset(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_env: None,
    ) -> None:
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
        monkeypatch.chdir(tmp_path)

        runtime = resolve_ssh_runtime_config()

        assert runtime.rtunnel_download_url == DEFAULT_RTUNNEL_DOWNLOAD_URL

    def test_bridge_rtunnel_url_uses_shared_resolution(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_env: None,
    ) -> None:
        _write_project_config(
            tmp_path,
            """
[cli]
prefer_source = "toml"

[ssh]
rtunnel_download_url = "https://project.example/shared.tgz"
""",
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_RTUNNEL_DOWNLOAD_URL", "https://env.example/shared.tgz")

        assert _get_rtunnel_download_url() == "https://project.example/shared.tgz"

    def test_rtunnel_upload_policy_defaults_to_auto(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_env: None,
    ) -> None:
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
        monkeypatch.chdir(tmp_path)

        runtime = resolve_ssh_runtime_config()

        assert runtime.rtunnel_upload_policy == "auto"

    def test_rtunnel_upload_policy_follows_project_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_env: None,
    ) -> None:
        _write_project_config(
            tmp_path,
            """
[ssh]
rtunnel_upload_policy = "never"
""",
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
        monkeypatch.chdir(tmp_path)

        runtime = resolve_ssh_runtime_config()

        assert runtime.rtunnel_upload_policy == "never"

    def test_rtunnel_upload_policy_env_overrides_by_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_env: None,
    ) -> None:
        _write_project_config(
            tmp_path,
            """
[ssh]
rtunnel_upload_policy = "never"
""",
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_RTUNNEL_UPLOAD_POLICY", "always")

        runtime = resolve_ssh_runtime_config()

        assert runtime.rtunnel_upload_policy == "always"

    def test_rtunnel_upload_policy_cli_override_has_highest_priority(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_env: None,
    ) -> None:
        _write_project_config(
            tmp_path,
            """
[ssh]
rtunnel_upload_policy = "never"
""",
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
        monkeypatch.chdir(tmp_path)

        runtime = resolve_ssh_runtime_config(cli_overrides={"rtunnel_upload_policy": "always"})

        assert runtime.rtunnel_upload_policy == "always"

    def test_rtunnel_upload_policy_invalid_raises_config_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_env: None,
    ) -> None:
        _write_project_config(
            tmp_path,
            """
[ssh]
rtunnel_upload_policy = "bogus"
""",
        )
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(ConfigError):
            resolve_ssh_runtime_config()
