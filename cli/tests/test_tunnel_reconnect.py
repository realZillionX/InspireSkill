from __future__ import annotations

from typing import Any

from inspire.bridge.tunnel import BridgeProfile, TunnelConfig
from inspire.cli.utils.tunnel_reconnect import (
    NotebookBridgeReconnectState,
    NotebookBridgeReconnectStatus,
    attempt_notebook_bridge_rebuild,
    should_attempt_ssh_reconnect,
)


def test_should_attempt_ssh_reconnect_interactive_only_by_default() -> None:
    assert should_attempt_ssh_reconnect(255, interactive=True) is True
    assert should_attempt_ssh_reconnect(255, interactive=False) is False


def test_should_attempt_ssh_reconnect_supports_non_interactive_opt_in() -> None:
    assert (
        should_attempt_ssh_reconnect(
            255,
            interactive=False,
            allow_non_interactive=True,
        )
        is True
    )
    assert (
        should_attempt_ssh_reconnect(
            1,
            interactive=True,
            allow_non_interactive=True,
        )
        is False
    )


def test_attempt_notebook_bridge_rebuild_returns_not_rebuildable_for_non_notebook_bridge() -> None:
    state = NotebookBridgeReconnectState(reconnect_limit=2, reconnect_pause=0.0)
    bridge = BridgeProfile(name="bridge", proxy_url="https://proxy.example")
    tunnel_config = TunnelConfig(bridges={"bridge": bridge}, default_bridge="bridge")
    calls = {"session": 0, "key": 0, "rebuild": 0}

    result = attempt_notebook_bridge_rebuild(
        state=state,
        bridge_name="bridge",
        bridge=bridge,
        tunnel_config=tunnel_config,
        session_loader=lambda: calls.__setitem__("session", calls["session"] + 1) or object(),
        key_loader=lambda path=None: calls.__setitem__("key", calls["key"] + 1) or "ssh-key",
        rebuild_fn=lambda **kwargs: calls.__setitem__("rebuild", calls["rebuild"] + 1) or bridge,
    )

    assert result.status is NotebookBridgeReconnectStatus.NOT_REBUILDABLE
    assert state.reconnect_attempt == 0


def test_attempt_notebook_bridge_rebuild_reuses_cached_material_after_retry() -> None:
    state = NotebookBridgeReconnectState(reconnect_limit=3, reconnect_pause=0.5)
    bridge = BridgeProfile(
        name="bridge",
        proxy_url="https://proxy.example/proxy/31337/",
        notebook_id="notebook-1",
    )
    tunnel_config = TunnelConfig(bridges={"bridge": bridge}, default_bridge="bridge")
    calls = {"session": 0, "key": 0, "rebuild": 0}

    def rebuild_fn(**kwargs: Any) -> BridgeProfile:
        calls["rebuild"] += 1
        if calls["rebuild"] == 1:
            raise RuntimeError("temporary failure")
        return bridge

    first = attempt_notebook_bridge_rebuild(
        state=state,
        bridge_name="bridge",
        bridge=bridge,
        tunnel_config=tunnel_config,
        session_loader=lambda: calls.__setitem__("session", calls["session"] + 1) or object(),
        key_loader=lambda path=None: calls.__setitem__("key", calls["key"] + 1) or "ssh-key",
        rebuild_fn=rebuild_fn,
    )
    second = attempt_notebook_bridge_rebuild(
        state=state,
        bridge_name="bridge",
        bridge=bridge,
        tunnel_config=tunnel_config,
        session_loader=lambda: calls.__setitem__("session", calls["session"] + 1) or object(),
        key_loader=lambda path=None: calls.__setitem__("key", calls["key"] + 1) or "ssh-key",
        rebuild_fn=rebuild_fn,
    )

    assert first.status is NotebookBridgeReconnectStatus.RETRY_LATER
    assert first.attempt == 1
    assert first.pause_seconds == 0.5
    assert second.status is NotebookBridgeReconnectStatus.REBUILT
    assert second.attempt == 2
    assert calls["session"] == 1
    assert calls["key"] == 1
    assert calls["rebuild"] == 2


def test_attempt_notebook_bridge_rebuild_returns_exhausted_on_last_failure() -> None:
    state = NotebookBridgeReconnectState(reconnect_limit=1, reconnect_pause=0.0)
    bridge = BridgeProfile(
        name="bridge",
        proxy_url="https://proxy.example/proxy/31337/",
        notebook_id="notebook-1",
    )
    tunnel_config = TunnelConfig(bridges={"bridge": bridge}, default_bridge="bridge")

    result = attempt_notebook_bridge_rebuild(
        state=state,
        bridge_name="bridge",
        bridge=bridge,
        tunnel_config=tunnel_config,
        session_loader=lambda: object(),
        key_loader=lambda path=None: "ssh-key",
        rebuild_fn=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert result.status is NotebookBridgeReconnectStatus.EXHAUSTED
    assert result.attempt == 1
    assert isinstance(result.error, RuntimeError)
