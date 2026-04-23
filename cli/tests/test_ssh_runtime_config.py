"""Tests for SSH runtime config resolution and rtunnel URL usage."""

from __future__ import annotations

from pathlib import Path

import pytest

from inspire.bridge.tunnel.rtunnel import _get_rtunnel_download_url
from inspire.config import Config
from inspire.config.ssh_runtime import DEFAULT_RTUNNEL_DOWNLOAD_URL, resolve_ssh_runtime_config


def _write_project_config(root: Path, content: str) -> None:
    project_dir = root / ".inspire"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "config.toml").write_text(content)


class TestSshRuntimeConfig:
    @pytest.fixture
    def clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env_vars = [
            "INSPIRE_SSHD_DEB_DIR",
            "INSPIRE_DROPBEAR_DEB_DIR",
            "INSPIRE_SETUP_SCRIPT",
            "INSPIRE_RTUNNEL_DOWNLOAD_URL",
            "INSPIRE_APT_MIRROR_URL",
        ]
        for var in env_vars:
            monkeypatch.delenv(var, raising=False)

    # ------------------------------------------------------------------
    # Layering: global TOML vs project TOML vs env var vs CLI override.
    # We test this on ``sshd_deb_dir`` because it's a simple Optional[str]
    # field with no validation magic, so any observed value change
    # unambiguously reflects the layering logic.
    # ------------------------------------------------------------------

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
sshd_deb_dir = "/project/sshd"
""",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_SSHD_DEB_DIR", "/env/sshd")

        runtime = resolve_ssh_runtime_config()

        assert runtime.sshd_deb_dir == "/env/sshd"

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
sshd_deb_dir = "/project/sshd"
""",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_SSHD_DEB_DIR", "/env/sshd")

        runtime = resolve_ssh_runtime_config()

        assert runtime.sshd_deb_dir == "/project/sshd"

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
sshd_deb_dir = "/global/sshd"
"""
        )

        _write_project_config(
            tmp_path,
            """
[cli]
prefer_source = "toml"
""",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_SSHD_DEB_DIR", "/env/sshd")

        runtime = resolve_ssh_runtime_config()

        assert runtime.sshd_deb_dir == "/env/sshd"

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
sshd_deb_dir = "/project/sshd"
""",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_SSHD_DEB_DIR", "/env/sshd")

        runtime = resolve_ssh_runtime_config(cli_overrides={"sshd_deb_dir": "/cli/sshd"})

        assert runtime.sshd_deb_dir == "/cli/sshd"

    # ------------------------------------------------------------------
    # rtunnel_download_url specifically (consumed by both notebook
    # bootstrap and the Mac-local bridge tunnel helper).
    # ------------------------------------------------------------------

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
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_RTUNNEL_DOWNLOAD_URL", "https://env.example/shared.tgz")

        assert _get_rtunnel_download_url() == "https://project.example/shared.tgz"
