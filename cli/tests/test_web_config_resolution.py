"""Tests for web-facing config resolution helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from inspire.cli.utils.notebook_cli import get_base_url
from inspire.config import Config
from inspire.platform.web.browser_api.notebooks import _config_compute_groups_fallback


def test_notebook_cli_base_url_respects_prefer_source_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = tmp_path / ".inspire"
    project_dir.mkdir()
    (project_dir / "config.toml").write_text(
        """
[cli]
prefer_source = "toml"

[api]
base_url = "https://toml.example"
"""
    )
    monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INSPIRE_BASE_URL", "https://env.example")

    assert get_base_url() == "https://toml.example"


def test_notebook_compute_group_fallback_uses_layered_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = tmp_path / ".inspire"
    project_dir.mkdir()
    (project_dir / "config.toml").write_text(
        """
[[compute_groups]]
name = "H200 A"
id = "lcg-test-1"
gpu_type = "H200"
"""
    )
    monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "missing" / "config.toml")
    monkeypatch.chdir(tmp_path)

    groups = _config_compute_groups_fallback()

    assert len(groups) == 1
    assert groups[0]["logic_compute_group_id"] == "lcg-test-1"
    assert groups[0]["name"] == "H200 A"
