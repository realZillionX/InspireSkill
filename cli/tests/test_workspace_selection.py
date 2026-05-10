"""Tests for workspace-name selection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id, workspace_required_hint

WS_SPECIAL = "ws-22222222-2222-2222-2222-222222222222"


def _cfg(**kwargs) -> Config:
    cfg = Config(username="", password="")
    for key, value in kwargs.items():
        setattr(cfg, key, value)
    return cfg


def test_no_arguments_returns_none() -> None:
    cfg = _cfg()
    assert select_workspace_id(cfg) is None


def test_no_default_workspace_field_in_config() -> None:
    """Sanity: the removed schema field is gone from Config."""
    assert not hasattr(Config(username="", password=""), "job_workspace_id")


def test_gpu_type_hint_is_silently_ignored() -> None:
    cfg = _cfg()
    assert select_workspace_id(cfg, gpu_type="H200") is None
    assert select_workspace_id(cfg, cpu_only=True) is None


def test_explicit_workspace_id_returns_directly() -> None:
    cfg = _cfg()
    explicit = "ws-11111111-1111-1111-1111-111111111111"
    assert select_workspace_id(cfg, explicit_workspace_id=explicit) == explicit


def test_explicit_workspace_name_uses_session_workspace_names() -> None:
    cfg = _cfg()
    session = SimpleNamespace(all_workspace_names={WS_SPECIAL: "special"})
    assert (
        select_workspace_id(cfg, explicit_workspace_name="special", session=session)
        == WS_SPECIAL
    )


def test_unknown_workspace_name_raises() -> None:
    cfg = _cfg()
    with pytest.raises(ConfigError, match="Unknown workspace name"):
        select_workspace_id(
            cfg,
            explicit_workspace_name="does-not-exist",
            session=SimpleNamespace(all_workspace_names={WS_SPECIAL: "special"}),
        )


def test_placeholder_workspace_id_is_rejected_when_explicit() -> None:
    cfg = _cfg()
    with pytest.raises(ConfigError, match="placeholder"):
        select_workspace_id(
            cfg,
            explicit_workspace_id="ws-00000000-0000-0000-0000-000000000000",
        )


def test_workspace_required_hint_points_to_live_context() -> None:
    cfg = _cfg()
    msg = workspace_required_hint(cfg)
    assert "--workspace <workspace-name>" in msg
    assert "inspire config context" in msg


def test_config_model_has_no_workspace_map() -> None:
    assert not hasattr(Config(username="", password=""), "workspaces")
