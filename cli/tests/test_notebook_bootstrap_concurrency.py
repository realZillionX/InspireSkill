from __future__ import annotations

import importlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from inspire.bridge.tunnel import BridgeProfile, TunnelConfig
from inspire.cli.commands.notebook.target_resolver import NotebookConnectionTarget
from inspire.cli.commands.notebook.transport import NotebookTransportPolicy
from inspire.cli.context import Context

ssh_proxy_module = importlib.import_module("inspire.cli.commands.notebook.ssh_proxy_cmd")


def test_concurrent_proxy_calls_bootstrap_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: two ProxyCommand calls observe the same cached tunnel as unavailable.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bridge = BridgeProfile(
        name="demo-box",
        proxy_url="https://proxy.invalid/proxy/31337/",
        notebook_name="demo-box",
        workspace_name="CPU资源空间",
    )
    config = TunnelConfig(account="alice")
    config.add_bridge(bridge)
    target = NotebookConnectionTarget(
        account="alice",
        config=config,
        bridge=bridge,
        source="target_cache",
    )
    initial_checks = threading.Barrier(2)
    state_lock = threading.Lock()
    state = {"ready": False, "checks": 0, "bootstraps": 0, "proxies": 0}

    def fake_is_tunnel_available(**_kwargs) -> bool:  # noqa: ANN003
        with state_lock:
            observed_ready = state["ready"]
            state["checks"] += 1
            check_number = state["checks"]
        if check_number <= 2:
            initial_checks.wait()
        return bool(observed_ready)

    def fake_run_notebook_ssh(_ctx: Context, **_kwargs) -> None:  # noqa: ANN003
        with state_lock:
            state["bootstraps"] += 1
            state["ready"] = True

    def fake_exec_rtunnel_proxy(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        with state_lock:
            state["proxies"] += 1

    monkeypatch.setattr(ssh_proxy_module, "_load_proxy_target", lambda *_a, **_k: target)
    monkeypatch.setattr(ssh_proxy_module, "is_tunnel_available", fake_is_tunnel_available)
    monkeypatch.setattr(ssh_proxy_module, "run_notebook_ssh", fake_run_notebook_ssh)
    monkeypatch.setattr(ssh_proxy_module, "exec_rtunnel_proxy", fake_exec_rtunnel_proxy)
    monkeypatch.setattr(
        ssh_proxy_module,
        "preflight_notebook_transport_policy",
        lambda *_a, **_k: NotebookTransportPolicy(
            notebook="demo-box",
            notebook_id="notebook-demo",
            compute_group="CPU资源-2",
        ),
    )

    callback = ssh_proxy_module.ssh_proxy_cmd.callback
    assert callback is not None
    command = callback.__wrapped__

    def invoke_proxy() -> None:
        command(
            Context(),
            notebook="demo-box",
            workspace="CPU资源空间",
            account="alice",
            pick=None,
            ignore_target_cache=False,
            ssh_port=22222,
            connection_port=31337,
            pubkey=None,
            setup_timeout=30,
            quiet=False,
        )

    # When: both cold calls proceed after their initial readiness check.
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(invoke_proxy) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)

    # Then: only the lock owner prepares the connection; both calls proxy it.
    assert state["bootstraps"] == 1
    assert state["proxies"] == 2
