"""SDK local I/O workers are bounded, isolated from the host, and joined on close."""
from __future__ import annotations

import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
import threading

import httpx
import pytest

from inspire.sdk import InspireAsyncClient
from inspire.sdk.resources import Workspaces
from inspire.platform.web.flow import blocking_io, call, perform_sync
from inspire.platform.web.offload import LOCAL_IO_WORKERS, current_pool, offload
from test_sdk import client as client
from test_sdk_async import tracked as tracked

pytestmark = pytest.mark.timeout(30, method="thread")


async def wait_event(event):
    # Never borrow the default executor just to wait for a test's thread event.
    async def poll():
        while not event.is_set():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 2)


def test_local_pool_is_bounded_reused_and_threads_return_to_baseline(tracked, monkeypatch):
    workers = set()
    release, full = threading.Event(), threading.Event()
    lock = threading.Lock()

    @blocking_io
    def local_read():
        with lock:
            workers.add(threading.current_thread())
            if len(workers) == LOCAL_IO_WORKERS:
                full.set()
        assert release.wait(5), "test did not release local I/O"
        return []

    monkeypatch.setattr(Workspaces, "_all", lambda self: local_read())

    async def run():
        baseline = set(threading.enumerate())
        c = InspireAsyncClient("alpha")
        assert set(threading.enumerate()) == baseline, "workers must start lazily"
        try:
            tasks = [asyncio.create_task(c.workspaces.list()) for _ in range(16)]
            await wait_event(full)
            assert len(workers) == LOCAL_IO_WORKERS == 4
            assert len(set(threading.enumerate()) - baseline) == 4
            release.set()
            await asyncio.gather(*tasks)
            first_workers = workers.copy()
            for _ in range(8):
                await c.workspaces.list()
            await asyncio.gather(*(c.workspaces.list() for _ in range(16)))
            assert workers == first_workers, "calls must reuse the same bounded pool"
            await c.close()
            # Check before asyncio.run can shut down the loop/default executor.
            assert set(threading.enumerate()) == baseline, "SDK threads survived client.close()"
            assert all(not thread.is_alive() for thread in workers)
            await c.close()
        finally:
            release.set()
            await c.close()
            # Mutation tests must not leave deliberately leaked workers behind.
            c._offload_pool._executor.shutdown(wait=True, cancel_futures=True)

    asyncio.run(run())


def test_sdk_does_not_consume_or_wait_for_callers_default_executor(tracked, monkeypatch):
    submissions = []
    host_started, host_release = threading.Event(), threading.Event()
    local_threads = []

    class HostPool(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            submissions.append(fn)
            return super().submit(fn, *args, **kwargs)

    def host_work():
        host_started.set()
        assert host_release.wait(5)
        return threading.current_thread()

    @blocking_io
    def local_read():
        local_threads.append(threading.current_thread())
        return []

    def rows(self):
        local_read()
        assert self.client._transport.request("GET", "/fake")["ok"]
        return []

    async def send(http, request, **kwargs):
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(Workspaces, "_all", rows)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)

    async def run():
        loop = asyncio.get_running_loop()
        host = HostPool(max_workers=1, thread_name_prefix="application-only")
        loop.set_default_executor(host)
        task = asyncio.create_task(asyncio.to_thread(host_work))
        try:
            await wait_event(host_started)
            async with InspireAsyncClient("alpha") as c:
                await asyncio.wait_for(asyncio.gather(*(c.workspaces.list() for _ in range(8))), 2)
            assert len(submissions) == 1, "SDK submitted work to the host's saturated executor"
            assert local_threads and all(t.name.startswith("inspire-local-io") for t in local_threads)
            host_release.set()
            host_thread = await task
            assert host_thread.is_alive(), "closing SDK must not close the application's executor"
            assert await asyncio.to_thread(threading.current_thread) is host_thread
            assert len(submissions) == 2
        finally:
            host_release.set()
            await task
            await loop.shutdown_default_executor()

    asyncio.run(run())


def test_cancelled_offload_returns_promptly_but_close_joins_running_thread(tracked, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def prepare():
        entered.set()
        try:
            assert release.wait(5)
            return []
        finally:
            finished.set()

    async def read():
        return await offload(prepare)

    monkeypatch.setattr(Workspaces, "_all", lambda self: perform_sync(call(read)))

    async def run():
        c = InspireAsyncClient("alpha")
        task = asyncio.create_task(c.workspaces.list())
        closing = None
        try:
            await wait_event(entered)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert not finished.is_set(), "cancellation cannot interrupt the running thread"
            assert (await c.cache.stats())["entries"] == 0
            closing = asyncio.create_task(c.close())
            await asyncio.sleep(0.02)
            assert not closing.done(), "close must join the still-running local I/O"
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 2)
            assert finished.is_set()
        finally:
            release.set()
            await asyncio.gather(task, *([closing] if closing else []), return_exceptions=True)
            await c.close()

    asyncio.run(run())


def test_clients_have_separate_pools_and_offloads_copy_context(tracked, monkeypatch):
    context = ContextVar("test_sdk_caller", default="outside")

    @blocking_io
    def read():
        value = (context.get(), current_pool.get(), threading.current_thread())
        context.set("worker-only")
        return value

    monkeypatch.setattr(Workspaces, "get", lambda self, **kw: read())

    async def run():
        async with InspireAsyncClient("alpha") as first, InspireAsyncClient("alpha") as second:
            assert current_pool.get() is None
            context.set("caller")
            a, b = await asyncio.gather(first.workspaces.get("a"), second.workspaces.get("b"))
            assert a[:2] == ("caller", first._offload_pool)
            assert b[:2] == ("caller", second._offload_pool)
            assert a[2] is not b[2]
            assert context.get() == "caller" and current_pool.get() is None
            await first.close()
            assert not a[2].is_alive() and b[2].is_alive()
            assert (await second.workspaces.get("again"))[2] is b[2]
        assert not b[2].is_alive()

    asyncio.run(run())


def test_async_offloads_cannot_bypass_client_pool():
    # All explicit offload sites go through one context-aware gateway. This also
    # catches new sites outside the SDK package (websocket, browser, file output).
    violations = []
    for path in Path("inspire").rglob("*.py"):
        if "secrets" in path.parts or path.as_posix() == "inspire/platform/web/offload.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                name = (node.func.attr if isinstance(node.func, ast.Attribute)
                        else node.func.id if isinstance(node.func, ast.Name) else "")
                if name in {"to_thread", "run_in_executor"}:
                    violations.append(f"{path}:{node.lineno}: {name}")
    assert not violations, "Offload bypasses owned executor: " + ", ".join(violations)
