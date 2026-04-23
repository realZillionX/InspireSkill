"""Tests for workspace auto-selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id

WS_CPU = "ws-6e6ba362-e98e-45b2-9c5a-311998e93d65"
WS_GPU = "ws-9dcc0e1f-80a4-4af2-bc2f-0e352e7b17e6"
WS_SPECIAL = "ws-22222222-2222-2222-2222-222222222222"


def _cfg(**kwargs) -> Config:
    return Config(username="", password="", **kwargs)


def test_select_defaults_to_cpu_workspace() -> None:
    cfg = _cfg(workspace_cpu_id=WS_CPU, workspace_gpu_id=WS_GPU)
    assert select_workspace_id(cfg) == WS_CPU


def test_select_gpu_workspace_for_h200() -> None:
    cfg = _cfg(workspace_cpu_id=WS_CPU, workspace_gpu_id=WS_GPU)
    assert select_workspace_id(cfg, gpu_type="H200") == WS_GPU


def test_select_gpu_workspace_for_4090() -> None:
    """'internet' role is gone — 4090 routes to the same GPU workspace."""
    cfg = _cfg(workspace_cpu_id=WS_CPU, workspace_gpu_id=WS_GPU)
    assert select_workspace_id(cfg, gpu_type="4090") == WS_GPU


def test_select_cpu_for_cpu_only_requests() -> None:
    cfg = _cfg(workspace_cpu_id=WS_CPU, workspace_gpu_id=WS_GPU)
    assert select_workspace_id(cfg, cpu_only=True) == WS_CPU


def test_explicit_workspace_id_overrides() -> None:
    cfg = _cfg(workspace_cpu_id=WS_CPU)
    explicit = "ws-11111111-1111-1111-1111-111111111111"
    assert select_workspace_id(cfg, explicit_workspace_id=explicit) == explicit


def test_explicit_workspace_name_uses_workspaces_map() -> None:
    cfg = _cfg(workspaces={"special": WS_SPECIAL})
    assert select_workspace_id(cfg, explicit_workspace_name="special") == WS_SPECIAL


def test_placeholder_workspace_id_is_rejected() -> None:
    cfg = _cfg(workspace_cpu_id="ws-00000000-0000-0000-0000-000000000000")
    with pytest.raises(ConfigError, match="placeholder"):
        select_workspace_id(cfg)


def test_config_loads_workspaces_from_project_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".inspire").mkdir()

    (project_root / ".inspire" / "config.toml").write_text(
        """
[workspaces]
cpu = "ws-6e6ba362-e98e-45b2-9c5a-311998e93d65"
gpu = "ws-9dcc0e1f-80a4-4af2-bc2f-0e352e7b17e6"
special = "ws-22222222-2222-2222-2222-222222222222"
""".lstrip(),
        encoding="utf-8",
    )

    # Isolate from any real ~/.inspire/current.
    fake_home = tmp_path / "__home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.chdir(project_root)
    monkeypatch.delenv("INSPIRE_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("INSPIRE_WORKSPACE_CPU_ID", raising=False)
    monkeypatch.delenv("INSPIRE_WORKSPACE_GPU_ID", raising=False)

    cfg, _ = Config.from_files_and_env(require_credentials=False)
    assert cfg.workspace_cpu_id == WS_CPU
    assert cfg.workspace_gpu_id == WS_GPU
    assert cfg.workspaces.get("special") == WS_SPECIAL
