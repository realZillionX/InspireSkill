"""Tests for workspace selection.

The CLI no longer guesses workspace by GPU type or CPU-only hint — callers
must pass an explicit override, or the active account's ``[context].workspace``
/ ``INSPIRE_WORKSPACE_ID`` must point at a real workspace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id

WS_SPECIAL = "ws-22222222-2222-2222-2222-222222222222"
WS_DEFAULT = "ws-dddddddd-dddd-dddd-dddd-dddddddddddd"


def _cfg(**kwargs) -> Config:
    return Config(username="", password="", **kwargs)


def test_defaults_to_job_workspace_id() -> None:
    cfg = _cfg(job_workspace_id=WS_DEFAULT)
    assert select_workspace_id(cfg) == WS_DEFAULT


def test_no_default_returns_none() -> None:
    cfg = _cfg()
    assert select_workspace_id(cfg) is None


def test_gpu_type_hint_is_ignored() -> None:
    """The legacy ``gpu_type`` / ``cpu_only`` kwargs are silently ignored."""
    cfg = _cfg(job_workspace_id=WS_DEFAULT)
    assert select_workspace_id(cfg, gpu_type="H200") == WS_DEFAULT
    assert select_workspace_id(cfg, gpu_type="4090") == WS_DEFAULT
    assert select_workspace_id(cfg, cpu_only=True) == WS_DEFAULT


def test_explicit_workspace_id_overrides() -> None:
    cfg = _cfg(job_workspace_id=WS_DEFAULT)
    explicit = "ws-11111111-1111-1111-1111-111111111111"
    assert select_workspace_id(cfg, explicit_workspace_id=explicit) == explicit


def test_explicit_workspace_name_uses_workspaces_map() -> None:
    cfg = _cfg(workspaces={"special": WS_SPECIAL})
    assert select_workspace_id(cfg, explicit_workspace_name="special") == WS_SPECIAL


def test_unknown_workspace_name_raises() -> None:
    cfg = _cfg(workspaces={"special": WS_SPECIAL})
    with pytest.raises(ConfigError, match="Unknown workspace name"):
        select_workspace_id(cfg, explicit_workspace_name="does-not-exist")


def test_placeholder_workspace_id_is_rejected() -> None:
    cfg = _cfg(job_workspace_id="ws-00000000-0000-0000-0000-000000000000")
    with pytest.raises(ConfigError, match="placeholder"):
        select_workspace_id(cfg)


def test_config_loads_workspace_alias_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".inspire").mkdir()
    (project_root / ".inspire" / "config.toml").write_text(
        '[workspaces]\nspecial = "ws-22222222-2222-2222-2222-222222222222"\n',
        encoding="utf-8",
    )
    fake_home = tmp_path / "__home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.chdir(project_root)
    monkeypatch.delenv("INSPIRE_WORKSPACE_ID", raising=False)

    cfg, _ = Config.from_files_and_env(require_credentials=False)
    assert cfg.workspaces.get("special") == WS_SPECIAL
