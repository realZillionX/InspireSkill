"""Bridge reachability must leave local preparation workers available."""
from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time

import httpx
import pytest

from inspire.bridge.tunnel import ssh, process_async
from inspire.bridge.tunnel.models import BridgeProfile, TunnelConfig
from inspire.platform.web.flow import call, perform_sync
from inspire.platform.web.offload import LOCAL_IO_WORKERS
from inspire.sdk import InspireAsyncClient
from inspire.sdk.notebooks import Notebooks
from inspire.sdk.resources import Workspaces
from inspire.services.execution import remote_exec
from test_sdk import client as client
from test_sdk_async import tracked as tracked

pytestmark = pytest.mark.timeout(30, method="thread")


def test_bridge_waits_leave_http_preparation_available(client, tracked, monkeypatch):
    release = threading.Event()
    started = 0
    lock = threading.Lock()

    def enter():
        nonlocal started
        with lock:
            started += 1

    def blocking_process(*args, **kwargs):
        enter()
        assert release.wait(5)
        return subprocess.CompletedProcess(args[0], 0, "ok", "")

    async def native_process(*args, **kwargs):
        enter()
        while not release.is_set():
            await asyncio.sleep(0.001)
        return subprocess.CompletedProcess(args[0], 0, "ok", "")

    bridge = BridgeProfile("saved", "http://localhost", notebook_id="nb", workspace_id="ws")
    config = TunnelConfig(bridges={bridge.name: bridge})
    monkeypatch.setattr(remote_exec, "load_tunnel_config", lambda **kw: config)
    monkeypatch.setattr(remote_exec, "read_target_cache", lambda: {})
    monkeypatch.setattr(ssh, "_ensure_rtunnel_binary", lambda config: None)
    monkeypatch.setattr(ssh, "_get_proxy_command", lambda *a, **kw: "fake-proxy")
    monkeypatch.setattr(ssh, "build_ssh_process_env", lambda: {})
    monkeypatch.setattr(subprocess, "run", blocking_process)
    monkeypatch.setattr(process_async, "run_process", native_process)

    def execute(self, **kwargs):
        return perform_sync(call(remote_exec.cached_notebook_bridge,
                                 notebook_id="nb", workspace_id="ws", account="alpha"))

    async def send(http, request, **kwargs):
        return httpx.Response(200, json={"ok": True}, request=request)

    def rows(self):
        assert self.client._transport.request("GET", "/fake")["ok"]
        return []

    monkeypatch.setattr(Notebooks, "exec", execute)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(Workspaces, "_all", rows)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            probes = [asyncio.create_task(c.notebooks.exec("nb", command="true"))
                      for _ in range(LOCAL_IO_WORKERS)]
            try:
                # asyncio.timeout is 3.11+; this package supports 3.10.
                deadline = time.monotonic() + 2
                while started < LOCAL_IO_WORKERS:
                    assert time.monotonic() < deadline, "the probes never occupied the pool"
                    await asyncio.sleep(0.001)
                await asyncio.wait_for(c.workspaces.list(), 0.5)
                assert all(not task.done() for task in probes)
            finally:
                release.set()
                await asyncio.gather(*probes)

    asyncio.run(run())


@pytest.mark.skipif(
    os.name != "posix",
    reason="Executing the unchanged SSH probe argv via a shebang executable requires POSIX.",
)
def test_bridge_probe_runs_its_real_command_line_under_the_async_driver(
    client, tracked, monkeypatch, tmp_path
):
    """The driver swaps the subprocess primitive, so the arguments must still fit.

    Every other test here replaces the process call with a stand-in that accepts
    anything, which is exactly how a native runner missing one of subprocess.run's
    keywords stayed invisible: the probe detaches stdin, and the async path raised
    TypeError on the first real bridge.
    """
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text("#!/bin/sh\necho ok\n")
    fake_ssh.chmod(0o755)
    path = f"{tmp_path}{os.pathsep}{os.environ['PATH']}"
    monkeypatch.setenv("PATH", path)

    bridge = BridgeProfile("saved", "http://localhost", notebook_id="nb", workspace_id="ws")
    config = TunnelConfig(bridges={bridge.name: bridge})
    monkeypatch.setattr(remote_exec, "load_tunnel_config", lambda **kw: config)
    monkeypatch.setattr(remote_exec, "read_target_cache", lambda: {})
    monkeypatch.setattr(ssh, "_ensure_rtunnel_binary", lambda config: None)
    monkeypatch.setattr(ssh, "_get_proxy_command", lambda *a, **kw: "true")
    monkeypatch.setattr(ssh, "build_ssh_process_env", lambda: {"PATH": path})

    def execute(self, **kwargs):
        return perform_sync(call(remote_exec.cached_notebook_bridge,
                                 notebook_id="nb", workspace_id="ws", account="alpha"))

    monkeypatch.setattr(Notebooks, "exec", execute)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            return await c.notebooks.exec("nb", command="true")

    assert asyncio.run(run()) == "saved"
    assert ssh._test_ssh_connection(bridge, config) is True
