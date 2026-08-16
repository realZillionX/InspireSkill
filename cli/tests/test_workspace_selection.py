"""Tests for workspace-name selection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from inspire.accounts import create_account, set_current_account
from inspire.cli.utils.resource_index import ResourceIdentity, ResourceIndex, ResourceScope
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id, workspace_required_hint

WS_SPECIAL = "ws-22222222-2222-2222-2222-222222222222"
WS_RECREATED = "ws-33333333-3333-3333-3333-333333333333"


def _cfg(**kwargs) -> Config:
    cfg = Config(username="", password="")
    for key, value in kwargs.items():
        setattr(cfg, key, value)
    return cfg


def test_no_arguments_returns_none() -> None:
    assert select_workspace_id() is None


def test_explicit_workspace_id_returns_directly() -> None:
    explicit = "ws-11111111-1111-1111-1111-111111111111"
    assert select_workspace_id(explicit_workspace_id=explicit) == explicit


def test_explicit_workspace_name_uses_session_workspace_names() -> None:
    session = SimpleNamespace(all_workspace_names={WS_SPECIAL: "special"})
    assert (
        select_workspace_id(explicit_workspace_name="special", session=session)
        == WS_SPECIAL
    )


def test_explicit_workspace_name_uses_fresh_local_index(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    create_account("alpha", "[inspire]\n")
    set_current_account("alpha")
    index = ResourceIndex.for_account()
    assert index is not None
    scope = ResourceScope(
        base_url="https://inspire.example",
        subject_id="user-one",
        resource_type="workspace",
    )
    index.upsert(
        scope,
        [ResourceIdentity(resource_id=WS_SPECIAL, name="special")],
    )
    session = SimpleNamespace(
        base_url="https://inspire.example",
        user_detail={"id": "user-one"},
        login_username="alice",
        all_workspace_names=None,
    )
    monkeypatch.setattr(
        "inspire.config.workspaces.workspace_name_map",
        lambda _session: pytest.fail("fresh cache should avoid live workspace discovery"),
    )

    assert (
        select_workspace_id(
            explicit_workspace_name="SPECIAL",
            session=session,
        )
        == WS_SPECIAL
    )


def test_live_workspace_snapshot_cannot_overwrite_newer_write_through(
    tmp_path,
    monkeypatch,
) -> None:
    index = ResourceIndex(tmp_path / "resource-index.sqlite3")
    scope = ResourceScope(
        base_url="https://inspire.example",
        subject_id="user-one",
        resource_type="workspace",
    )
    index.upsert(
        scope,
        [ResourceIdentity(resource_id=WS_SPECIAL, name="special")],
        ttl_seconds=0,
    )
    session = SimpleNamespace(
        base_url="https://inspire.example",
        user_detail={"id": "user-one"},
        login_username="alice",
        all_workspace_names=None,
    )
    monkeypatch.setattr(
        ResourceIndex,
        "for_account",
        classmethod(lambda cls, account=None: index),
    )

    def _stale_live_names(_session):
        index.mark_deleted(scope, resource_id=WS_SPECIAL)
        index.upsert(
            scope,
            [ResourceIdentity(resource_id=WS_RECREATED, name="special")],
        )
        return {WS_SPECIAL: "special"}

    monkeypatch.setattr(
        "inspire.config.workspaces.workspace_name_map",
        _stale_live_names,
    )

    assert (
        select_workspace_id(
            explicit_workspace_name="special",
            session=session,
        )
        == WS_RECREATED
    )
    assert [
        item.resource_id
        for item in index.lookup(scope, "special", fresh_only=False)
    ] == [WS_RECREATED]


def test_clear_during_live_workspace_lookup_does_not_repopulate_cache(
    tmp_path,
    monkeypatch,
) -> None:
    index = ResourceIndex(tmp_path / "resource-index.sqlite3")
    scope = ResourceScope(
        base_url="https://inspire.example",
        subject_id="user-one",
        resource_type="workspace",
    )
    session = SimpleNamespace(
        base_url="https://inspire.example",
        user_detail={"id": "user-one"},
        login_username="alice",
        all_workspace_names=None,
    )
    monkeypatch.setattr(
        ResourceIndex,
        "for_account",
        classmethod(lambda cls, account=None: index),
    )

    def _live_names(_session):
        index.clear()
        return {WS_SPECIAL: "special"}

    monkeypatch.setattr(
        "inspire.config.workspaces.workspace_name_map",
        _live_names,
    )

    assert (
        select_workspace_id(
            explicit_workspace_name="special",
            session=session,
        )
        == WS_SPECIAL
    )
    assert index.list_identities(scope, fresh_only=False) == []


def test_workspace_snapshot_failure_skips_live_cache_write(
    tmp_path,
    monkeypatch,
) -> None:
    index = ResourceIndex(tmp_path / "resource-index.sqlite3")
    scope = ResourceScope(
        base_url="https://inspire.example",
        subject_id="user-one",
        resource_type="workspace",
    )
    session = SimpleNamespace(
        base_url="https://inspire.example",
        user_detail={"id": "user-one"},
        login_username="alice",
        all_workspace_names=None,
    )
    monkeypatch.setattr(
        ResourceIndex,
        "for_account",
        classmethod(lambda cls, account=None: index),
    )
    monkeypatch.setattr(
        index,
        "snapshot_token",
        lambda _scope: (_ for _ in ()).throw(OSError("cache unavailable")),
    )
    monkeypatch.setattr(
        "inspire.config.workspaces.workspace_name_map",
        lambda _session: {WS_SPECIAL: "special"},
    )

    assert (
        select_workspace_id(
            explicit_workspace_name="special",
            session=session,
        )
        == WS_SPECIAL
    )
    assert index.list_identities(scope, fresh_only=False) == []


def test_unknown_workspace_name_raises() -> None:
    with pytest.raises(ConfigError, match="Unknown workspace name"):
        select_workspace_id(
            explicit_workspace_name="does-not-exist",
            session=SimpleNamespace(all_workspace_names={WS_SPECIAL: "special"}),
        )


def test_invalid_workspace_selection_is_rejected_when_explicit() -> None:
    with pytest.raises(ConfigError, match="Workspace selection is invalid\\."):
        select_workspace_id(
            explicit_workspace_id="ws-00000000-0000-0000-0000-000000000000",
        )


def test_workspace_required_hint_points_to_live_context() -> None:
    cfg = _cfg()
    msg = workspace_required_hint(cfg)
    assert "--workspace <workspace-name>" in msg
    assert "inspire account context" in msg


def test_config_model_has_no_workspace_map() -> None:
    assert not hasattr(Config(username="", password=""), "workspaces")
