"""Async contracts on local accounts and fake backends only."""
from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator, Iterator, Awaitable, Callable
from contextlib import aclosing
import inspect
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import types
from typing import TypeVar, Union, get_args, get_origin, get_type_hints

import pytest
import inspire
from inspire.sdk import (
    Accounts, ClientClosedError, ClientThreadError, EventResult, InspireAsyncClient,
    InspireClient, Resource, ValidationError, WorkspaceRef,
)
from inspire.sdk import _async_runtime
from inspire.sdk.resources import Workspaces
from inspire.platform.web.flow import call, perform_sync
import httpx
from test_sdk import client as client
from test_sdk_signatures import facade_methods


# Thread watchdog also stops deadlocked event loops and cancellation cleanup.
pytestmark = pytest.mark.timeout(30, method="thread")


def specialize(value, bindings):
    if isinstance(value, TypeVar):
        return bindings[value]
    origin, args = get_origin(value), get_args(value)
    if not args:
        return value
    args = tuple(specialize(arg, bindings) for arg in args)
    if origin in (types.UnionType, Union):
        origin = Union
    if origin is Iterator:
        origin = AsyncIterator
    return origin, args


# Intentional divergences: submissions return client-bound async handle subclasses.
# Models.register has no wait contract and is deliberately absent.
HANDLE_CASES = [
    ("jobs", "create", "JobHandle", "JobRef", "wait", "bind_ref"),
    ("hpc", "create", "HPCJobHandle", "HPCJobRef", "wait", "bind_ref"),
    ("ray", "create", "RayJobHandle", "RayJobRef", "wait", "bind_ref"),
    ("servings", "create", "ServingHandle", "ServingRef", "wait", "bind_ref"),
    ("tensorboards", "create", "TensorboardHandle", "TensorboardRef", "wait", "bind_ref"),
    ("notebooks", "create", "NotebookHandle", "NotebookRef", "wait", "bind_ref"),
    ("notebooks", "save_image", "ImageSaveHandle", "ImageRef", "wait_image_ready", "bind_image_ref"),
    ("images", "register", "ImageRegisterHandle", "ImageRef", "wait_ready", "bind_ref"),
]
ASYNC_HANDLE_RETURNS = {(facade, producer): getattr(inspire, "Async" + handle)
                        for facade, producer, handle, _, _, _ in HANDLE_CASES}


def test_every_facade_and_signature_has_typed_async_mirror(client):
    async_client = InspireAsyncClient("alpha")
    expected = {(facade, method): bound for facade, method, bound in facade_methods(client)}
    actual = {(facade, method): bound for facade, method, bound in facade_methods(async_client)}
    assert {key[0] for key in expected} == {key[0] for key in actual}
    assert set(actual) - set(expected) == {
        (name, "exec_stream") for name in ("jobs", "notebooks", "hpc", "ray", "servings")
    } | {(facade, binding) for facade, _, _, _, _, binding in HANDLE_CASES}
    for key, bound in expected.items():
        mirror = actual[key]
        bindings = {}
        for base in getattr(type(bound.__self__), "__orig_bases__", ()):
            bindings.update(zip(getattr(get_origin(base), "__parameters__", ()), get_args(base)))
        hints = {name: specialize(value, bindings) for name, value in get_type_hints(bound).items()}
        if "on_output" in hints:
            hints["on_output"] = specialize(Callable[[str], None | Awaitable[None]] | None, {})
        if key in ASYNC_HANDLE_RETURNS:
            assert issubclass(ASYNC_HANDLE_RETURNS[key], hints["return"])
            hints["return"] = ASYNC_HANDLE_RETURNS[key]
        assert {name: specialize(value, {}) for name, value in get_type_hints(mirror).items()} == hints, key
        sync_params = inspect.signature(bound).parameters
        async_params = inspect.signature(mirror).parameters
        assert list(sync_params) == list(async_params), key
        for name, param in sync_params.items():
            assert param.kind == async_params[name].kind, (key, name)
            assert param.default == async_params[name].default, (key, name)
        assert inspect.iscoroutinefunction(mirror) or inspect.isasyncgenfunction(mirror), key
    sync_constructor = inspect.signature(InspireClient).parameters
    async_constructor = dict(inspect.signature(InspireAsyncClient).parameters)
    assert async_constructor.pop("concurrency").default is None
    assert sync_constructor == async_constructor
    root_methods = {name for name, _ in inspect.getmembers(InspireClient, inspect.isroutine)
                    if not name.startswith("_")}
    assert root_methods == {"from_credentials", "login", "init", "close"}
    for name in root_methods:
        sync_sig = inspect.signature(getattr(InspireClient, name))
        async_sig = inspect.signature(getattr(InspireAsyncClient, name))
        assert sync_sig.parameters == async_sig.parameters
    assert InspireAsyncClient.accounts is InspireClient.accounts is Accounts
    assert inspire.InspireAsyncClient is InspireAsyncClient
    tree = ast.parse(Path(inspire.__file__).read_text())
    checking = next(node for node in tree.body if isinstance(node, ast.If))
    assert any(isinstance(node, ast.alias) and node.name == "InspireAsyncClient"
               for node in ast.walk(checking))


def test_checked_in_wrappers_are_current(monkeypatch, tmp_path):
    # The generator constructs a client with a fake account; Windows prepares
    # that account's cache directory before any storage lock can be acquired.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    generate = runpy.run_path("scripts/generate_sdk_async.py")["generate"]
    assert Path("inspire/sdk/async_client.py").read_text() == generate()


@pytest.fixture
def tracked(client, monkeypatch):
    records = []

    class TrackedClient(InspireClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._transport._session = client._transport._session
            self._transport._last_success = client._transport._last_success
            self.owner = threading.get_ident()
            self.touches = []
            self.closed = False
            records.append(self)
            check = self._transport.check

            def checked():
                self.touches.append(threading.get_ident())
                assert threading.get_ident() == self.owner
                check()

            self._transport.check = checked

        def close(self):
            assert threading.get_ident() == self.owner
            super().close()
            self.closed = True

    monkeypatch.setattr(_async_runtime, "InspireClient", TrackedClient)
    return records


def test_returns_sync_value_without_blocking_loop(client, tracked, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    rows = [Resource("workspace", WorkspaceRef("workspace", "alpha", client.base_url, "ws", "ws"))]

    async def read():
        entered.set()
        await release.wait()
        return rows

    def slow(self):
        return perform_sync(call(read))

    monkeypatch.setattr(Workspaces, "_all", slow)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            task = asyncio.create_task(c.workspaces.list())
            await entered.wait()
            release.set()
            result = await task
            with monkeypatch.context() as patch:
                patch.setattr(Workspaces, "_all", lambda self: rows)
                assert result == client.workspaces.list()
            assert c.account == "alpha" and c.base_url == client.base_url
        assert not c._active
        await c.close()
        with pytest.raises(ClientClosedError):
            await c.workspaces.list()

    asyncio.run(run())
    assert len(tracked) == 1
    assert tracked[0].closed and tracked[0].touches
    assert set(tracked[0].touches) == {threading.get_ident()}



@pytest.mark.parametrize("concurrency", [None, 1, 2])
def test_native_concurrency_without_workers(tracked, monkeypatch, concurrency):
    active = maximum = 0
    both = asyncio.Event()
    threads = []

    async def send(http, request, **kwargs):
        nonlocal active, maximum
        threads.append(threading.get_ident())
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 1)
        active -= 1
        return httpx.Response(200, json={"ok": True}, request=request)

    def rows(self):
        assert self.client._transport.request("GET", "/fake")["ok"]
        return []

    monkeypatch.setattr(Workspaces, "_all", rows)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    from inspire.platform.web.offload import OffloadPool
    original_offload = OffloadPool.run
    offloaded = []

    async def local_io_only(self, function, *args, **kwargs):
        assert function.__name__ in {"prepare", "build"}, "business/network offload"
        offloaded.append(function.__name__)
        return await original_offload(self, function, *args, **kwargs)

    monkeypatch.setattr(OffloadPool, "run", local_io_only)

    async def run():
        options = {} if concurrency is None else {"concurrency": concurrency}
        if concurrency is None:
            c = InspireAsyncClient("alpha", **options)
        else:
            with pytest.warns(DeprecationWarning, match="has no effect"):
                c = InspireAsyncClient("alpha", **options)
        async with c:
            await asyncio.gather(c.workspaces.list(), c.workspaces.list())
        assert not c._active and not hasattr(c, "_workers")

    asyncio.run(run())
    assert offloaded.count("prepare") == 2
    assert offloaded.count("build") == 1
    assert maximum == 2
    assert len(tracked) == 1 and tracked[0].closed
    assert threads == [threading.get_ident()] * 2



def test_exceptions_preserve_instance_type_and_message(tracked, monkeypatch):
    error = ValidationError("same fake failure")

    def fail(self):
        raise error

    monkeypatch.setattr(Workspaces, "_all", fail)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            with pytest.raises(ValidationError, match="same fake failure") as caught:
                await c.workspaces.list()
            assert caught.value is error

    asyncio.run(run())


@pytest.mark.parametrize("facade", ["jobs", "notebooks", "hpc", "ray", "servings"])
def test_follow_cancel_interrupts_native_sleep_and_closes_client(tracked, monkeypatch, facade):
    from inspire.sdk.jobs import Jobs
    from inspire.sdk.notebooks import Notebooks
    from inspire.sdk.hpc import HPC
    from inspire.sdk.ray import Ray
    from inspire.sdk.servings import Servings
    cls = {"jobs": Jobs, "notebooks": Notebooks, "hpc": HPC, "ray": Ray,
           "servings": Servings}[facade]
    polled = threading.Event()
    polls = []

    def resolve(self, ref, workspace):
        self.client._transport.check()
        return ref

    def events(self, ref, **kwargs):
        self.client._transport.check()
        polls.append(1)
        polled.set()
        return EventResult(())

    monkeypatch.setattr(cls, "_resolve", resolve)
    monkeypatch.setattr(cls, "events", events)
    if facade in {"hpc", "ray", "servings"}:
        monkeypatch.setattr(cls, "_follow_event_batch", events)
    if facade == "jobs":
        monkeypatch.setattr(cls, "get", lambda *a, **k: types.SimpleNamespace(status="RUNNING"))

    async def run():
        async with InspireAsyncClient("alpha") as c:
            stream = getattr(c, facade).follow_events("fake", interval=60)
            task = asyncio.create_task(anext(stream))
            while not polled.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            await stream.aclose()
        assert not c._active

    asyncio.run(run())
    assert polls == [1]
    assert all(c.closed for c in tracked)


def test_iterator_runs_next_and_close_on_event_loop(tracked, monkeypatch):
    from inspire.sdk.jobs import Jobs
    closed = threading.Event()

    def rows(self, **kwargs):
        try:
            for value in range(100):
                self.client._transport.check()
                yield value
        finally:
            self.client._transport.check()
            closed.set()

    monkeypatch.setattr(Jobs, "iter", rows)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            async with aclosing(c.jobs.iter("fake")) as stream:
                assert await anext(stream) == 0
            assert closed.is_set()
            assert await c.cache.stats() == {"hits": 0, "misses": 0, "entries": 0}

    asyncio.run(run())


def test_exec_callback_and_async_chunks(tracked, monkeypatch):
    from inspire.sdk.jobs import Jobs
    from inspire.services.execution.remote_exec import ExecResult
    callback_threads = []
    expected = ExecResult(output="onetwo", stdout="onetwo", stderr="", returncode=0,
                          completed=True, transport="fake")

    def execute(self, *, on_output, **kwargs):
        self.client._transport.check()
        for chunk in ("one", "two"):
            on_output(chunk)
        return expected

    monkeypatch.setattr(Jobs, "exec", execute)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            result = await c.jobs.exec("fake", command="fake",
                                       on_output=lambda _: callback_threads.append(threading.get_ident()))
            assert result is expected
            chunks = [chunk async for chunk in c.jobs.exec_stream("fake", command="fake")]
            assert chunks == ["one", "two"]

    asyncio.run(run())
    assert callback_threads == [tracked[0].owner] * 2


@pytest.mark.parametrize("concurrency", [0, -1, True, 1.5])
def test_invalid_concurrency(concurrency):
    with pytest.raises(ValidationError, match="positive integer"):
        InspireAsyncClient("alpha", concurrency=concurrency)


def test_process_and_loop_affinity(tracked, monkeypatch):
    async def run():
        async with InspireAsyncClient("alpha") as c:
            with monkeypatch.context() as patch:
                patch.setattr(c, "_pid", -1)
                with pytest.raises(ClientThreadError):
                    await c.cache.stats()
        return c

    c = asyncio.run(run())
    with pytest.raises(ClientThreadError):
        asyncio.run(c.close())


def test_unclosed_native_client_does_not_hold_interpreter_open():
    script = """
import asyncio
from unittest.mock import patch
from types import SimpleNamespace
from inspire.sdk import InspireAsyncClient
async def main():
    with patch('inspire.accounts.account_exists', return_value=True), patch(
        'inspire.config.Config.from_files_and_env',
        return_value=(SimpleNamespace(base_url='https://example.invalid', username='fake'), None),
    ):
        client = InspireAsyncClient('fake')
        await client.cache.stats()
asyncio.run(main())
"""
    subprocess.run([sys.executable, "-c", script], check=True, timeout=5)


def test_follow_logs_cancel_and_finite_iterator(tracked, monkeypatch):
    from inspire.sdk.jobs import Jobs
    from inspire.sdk import LogResult
    polled = threading.Event()

    def logs(self, *args, **kwargs):
        self.client._transport.check()
        polled.set()
        return LogResult("", (), "", "", False, 0, ())

    monkeypatch.setattr(Jobs, "_resolve", lambda self, ref, ws: ref)
    monkeypatch.setattr(Jobs, "logs", logs)
    monkeypatch.setattr(Jobs, "get", lambda *a, **k: types.SimpleNamespace(status="RUNNING"))

    async def run():
        async with InspireAsyncClient("alpha") as c:
            task = asyncio.create_task(anext(c.jobs.follow_logs("fake", interval=60)))
            while not polled.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            # Client remains usable after iteration cancellation.
            assert (await c.cache.stats())["entries"] == 0
        assert not c._active

    asyncio.run(run())


def test_stream_error_and_early_exec_close(tracked, monkeypatch):
    from inspire.sdk.jobs import Jobs
    closed = threading.Event()
    error = ValidationError("stream failure")

    def fail(self, **kwargs):
        yield "before"
        raise error

    monkeypatch.setattr(Jobs, "iter", fail)

    def execute(self, *, on_output, **kwargs):
        try:
            for _ in range(100000):
                self.client._transport.check()
                on_output("chunk")
        finally:
            closed.set()

    monkeypatch.setattr(Jobs, "exec", execute)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            stream = c.jobs.iter("fake")
            assert await anext(stream) == "before"
            with pytest.raises(ValidationError) as caught:
                await anext(stream)
            assert caught.value is error
            async with aclosing(c.jobs.exec_stream("fake", command="fake")) as chunks:
                assert await anext(chunks) == "chunk"
            assert closed.is_set()

    asyncio.run(run())


def test_partial_initialization_failure_closes_native_client(tracked, monkeypatch):
    factory = _async_runtime.InspireClient

    def create(**options):
        result = factory(**options)
        del result._account
        return result

    monkeypatch.setattr(_async_runtime, "InspireClient", create)

    async def run():
        c = InspireAsyncClient("alpha")
        with pytest.raises(AttributeError):
            await c.__aenter__()
        assert tracked[0].closed and c._options == {}
        await c.close()
        with pytest.raises(ClientClosedError):
            await c.cache.stats()

    asyncio.run(run())



def test_close_cancels_active_follow_and_request(tracked, monkeypatch):
    from inspire.sdk.notebooks import Notebooks
    polled, entered = asyncio.Event(), asyncio.Event()
    stopped = asyncio.Event()

    def events(self, *args, **kwargs):
        polled.set()
        return EventResult(())

    async def send(http, request, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(Notebooks, "_resolve", lambda self, ref, ws: ref)
    monkeypatch.setattr(Notebooks, "events", events)
    monkeypatch.setattr(Workspaces, "_all", lambda self: self.client._transport.request("GET", "/fake"))
    monkeypatch.setattr(httpx.AsyncClient, "send", send)

    async def run():
        c = InspireAsyncClient("alpha")
        follow = asyncio.create_task(anext(c.notebooks.follow_events("fake", interval=60)))
        await polled.wait()
        request = asyncio.create_task(c.workspaces.list())
        await entered.wait()
        await asyncio.wait_for(c.close(), 1)
        for task in (follow, request):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stopped.is_set() and not c._active
        with pytest.raises(ClientClosedError):
            await c.workspaces.list()

    asyncio.run(run())



def test_cancelled_close_still_finishes_native_cleanup(tracked, monkeypatch):
    original = _async_runtime.AsyncRuntime._shutdown
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_close(self):
        entered.set()
        await release.wait()
        await original(self)

    monkeypatch.setattr(_async_runtime.AsyncRuntime, "_shutdown", slow_close)

    async def run():
        c = InspireAsyncClient("alpha")
        await c.__aenter__()
        closing = asyncio.create_task(c.close())
        await entered.wait()
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not c._active and tracked[0].closed
        await c.close()

    asyncio.run(run())



def test_cache_controls_cover_concurrent_calls(tracked, monkeypatch):
    def rows(self):
        self.client.cache._get(("fake",), lambda: "snapshot")
        return []

    monkeypatch.setattr(Workspaces, "_all", rows)

    async def run():
        async with InspireAsyncClient("alpha", concurrency=None) as c:
            await asyncio.gather(c.workspaces.list(), c.workspaces.list())
            assert (await c.cache.stats())["entries"] == 1
            await c.cache.clear()
            assert (await c.cache.stats())["entries"] == 0

    asyncio.run(run())


def test_credentials_factory_initializes_once_on_event_loop(tracked, monkeypatch):
    calls = []

    def credentials(account, **options):
        calls.append((threading.get_ident(), account, options))
        return "alpha"

    monkeypatch.setattr("inspire.sdk.client.ensure_credentials", credentials)

    async def run():
        c = InspireAsyncClient.from_credentials("fake", "unused", concurrency=None)
        assert calls == [] and tracked == []
        async with c:
            assert c.account == "alpha"
        assert len(calls) == 1 and calls[0][0] == tracked[0].owner

    asyncio.run(run())


def test_cancelled_first_request_cleans_up(tracked, monkeypatch):
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def send(http, request, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(Workspaces, "_all", lambda self: self.client._transport.request("GET", "/fake"))

    async def run():
        c = InspireAsyncClient("alpha")
        task = asyncio.create_task(c.workspaces.list())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert stopped.is_set() and not c._active
        assert (await c.cache.stats())["entries"] == 0
        await c.close()
        assert tracked[0].closed

    asyncio.run(run())



def test_exec_stream_close_interrupts_blocked_producer(tracked, monkeypatch):
    from inspire.sdk.jobs import Jobs

    exited = threading.Event()

    def execute(self, *args, on_output, **kwargs):
        try:
            while True:
                on_output("chunk")
        finally:
            exited.set()

    monkeypatch.setattr(Jobs, "exec", execute)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            async with aclosing(c.jobs.exec_stream("fake", command="fake")) as stream:
                assert await anext(stream) == "chunk"
            assert exited.is_set()
            assert (await c.cache.stats())["entries"] == 0
        assert not c._active

    asyncio.run(run())


@pytest.mark.parametrize("facade", ["ray", "servings"])
def test_status_native_reads_preserve_models_order_and_duplicates(client, tracked, monkeypatch, facade):
    service = getattr(client, facade)
    cls = type(service)
    refs = [service._make_ref(service._ref_type, key, key, "ws") for key in ("slow", "fast", "slow")]
    rows = {key: {"id": key, "name": key, "status": "RUNNING", "workspace_id": "ws"}
            for key in ("slow", "fast")}
    with monkeypatch.context() as patch:
        patch.setattr(cls, "_detail", lambda self, key: rows[key])
        expected = service.status(refs)
    started, completed = [], []
    all_started = asyncio.Event()

    async def read(key):
        started.append(key)
        if len(started) == 3:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), 1)
        if key == "slow":
            await asyncio.sleep(0.02)
        completed.append(key)
        return rows[key]

    monkeypatch.setattr(cls, "_detail", lambda self, key: perform_sync(call(read, key)))

    async def run():
        async with InspireAsyncClient("alpha") as c:
            result = await getattr(c, facade).status(refs)
            assert result == expected
            assert [item.ref.key for item in result] == ["slow", "fast", "slow"]
            assert await getattr(c, facade).status([]) == ()

    asyncio.run(run())
    assert started == ["slow", "fast", "slow"]
    assert completed[0] == "fast"


@pytest.mark.parametrize("facade", ["ray", "servings"])
def test_status_first_error_is_in_input_order(client, tracked, monkeypatch, facade):
    from inspire.sdk import ResourceNotFoundError
    service = getattr(client, facade)
    cls = type(service)
    refs = [service._make_ref(service._ref_type, key, key, "ws") for key in ("missing", "later")]
    with monkeypatch.context() as patch:
        patch.setattr(cls, "_detail", lambda self, key: {})
        with pytest.raises(ResourceNotFoundError) as expected:
            service.status(refs)
    later_failed = asyncio.Event()

    async def read(key):
        if key == "missing":
            await asyncio.wait_for(later_failed.wait(), 1)
            return {}
        later_failed.set()
        raise ValidationError("later fast error")

    monkeypatch.setattr(cls, "_detail", lambda self, key: perform_sync(call(read, key)))

    async def run():
        async with InspireAsyncClient("alpha") as c:
            with pytest.raises(type(expected.value)) as caught:
                await getattr(c, facade).status(refs)
            assert str(caught.value) == str(expected.value)
            assert not c._active

    asyncio.run(run())


@pytest.mark.parametrize("facade", ["ray", "servings"])
def test_status_bounds_reads_and_cancels_all_io(client, tracked, monkeypatch, facade):
    service = getattr(client, facade)
    refs = [service._make_ref(service._ref_type, str(i), str(i), "ws") for i in range(100)]
    started, stopped = [], []
    full = asyncio.Event()

    async def read(key):
        started.append(key)
        if len(started) == _async_runtime.STATUS_CONCURRENCY:
            full.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(key)

    monkeypatch.setattr(type(service), "_detail", lambda self, key: perform_sync(call(read, key)))

    async def run():
        async with InspireAsyncClient("alpha") as c:
            task = asyncio.create_task(getattr(c, facade).status(refs))
            await asyncio.wait_for(full.wait(), 1)
            await asyncio.sleep(0)
            assert len(started) == 8
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert sorted(stopped) == sorted(started)

    asyncio.run(run())


def test_iteration_cancellation_stops_underlying_http(tracked, monkeypatch):
    from inspire.sdk.jobs import Jobs
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def send(http, request, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    def rows(self, **kwargs):
        self.client._transport.request("GET", "/fake")
        yield "unreachable"

    monkeypatch.setattr(Jobs, "iter", rows)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            async with aclosing(c.jobs.iter("fake")) as stream:
                task = asyncio.create_task(anext(stream))
                await entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                assert stopped.is_set()

    asyncio.run(run())


def test_open_stream_allows_calls_and_cache_access(tracked, monkeypatch):
    from inspire.sdk.jobs import Jobs

    def rows(self, **kwargs):
        yield "one"
        yield "two"

    monkeypatch.setattr(Jobs, "iter", rows)
    monkeypatch.setattr(Workspaces, "_all", lambda self: [])

    async def run():
        async with InspireAsyncClient("alpha") as c:
            async with aclosing(c.jobs.iter("fake")) as stream:
                assert await anext(stream) == "one"
                assert (await asyncio.wait_for(c.workspaces.list(), 1)).items == ()
                await asyncio.wait_for(c.cache.clear(), 1)
                assert (await asyncio.wait_for(c.cache.stats(), 1))["entries"] == 0
                assert await anext(stream) == "two"

    asyncio.run(run())


def test_concurrent_writes_have_separate_single_send_and_deadline_state(tracked, monkeypatch):
    from inspire.sdk.jobs import Jobs
    from inspire.sdk.resources import operation
    entered = asyncio.Event()
    writes = []

    async def send(http, request, **kwargs):
        writes.append(str(request.url))
        if len(writes) == 2:
            entered.set()
        await asyncio.wait_for(entered.wait(), 1)
        return httpx.Response(200, json={}, request=request)

    @operation
    def stop(self, ref, **kwargs):
        owner = self.client._transport
        with owner.single_send(ref):
            deadline = owner.deadline
            owner.request("POST", "/" + ref)
            assert owner._write["operation_id"] == ref
            assert owner.deadline == deadline

    monkeypatch.setattr(Jobs, "stop", stop)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            await asyncio.gather(c.jobs.stop("first"), c.jobs.stop("second"))

    asyncio.run(run())
    assert len(writes) == 2


@pytest.mark.parametrize("transport", ["jupyter", "ssh"])
def test_notebook_exec_uses_native_io_and_loop_callbacks(tracked, monkeypatch, transport):
    from inspire.sdk.notebooks import Notebooks
    from inspire.services.execution import remote_exec as core
    from inspire.services.execution.async_output import deliver_output
    threads = []
    closed = asyncio.Event()
    result = core.ExecResult(0, "onetwo", "onetwo", "", True, transport)

    def resolve(self, ref, workspace):
        return types.SimpleNamespace(key="fake", workspace_id="ws", name="fake")

    def execute(*, on_output, **kwargs):
        threads.append(threading.get_ident())
        on_output("one")
        on_output("two")
        return result

    async def native(*, on_output, **kwargs):
        threads.append(threading.get_ident())
        try:
            await deliver_output(on_output, "one")
            await deliver_output(on_output, "two")
            return result
        finally:
            closed.set()

    monkeypatch.setattr(Notebooks, "_resolve", resolve)
    monkeypatch.setattr(core, "cached_notebook_bridge", lambda **kw: "bridge")
    monkeypatch.setattr(core, "exec_in_notebook_ssh", execute)
    monkeypatch.setattr(core, "exec_in_notebook_jupyter_async", native)
    if transport == "jupyter":
        monkeypatch.setattr(_async_runtime.OffloadPool, "run", lambda *a, **k: pytest.fail("native exec offloaded"))

    async def run():
        async with InspireAsyncClient("alpha") as c:
            chunks = []
            assert await c.notebooks.exec("fake", command="fake", transport=transport,
                                          on_output=chunks.append) is result
            assert chunks == ["one", "two"]
            chunks = [chunk async for chunk in c.notebooks.exec_stream(
                "fake", command="fake", transport=transport,
            )]
            assert chunks == ["one", "two"]
            if transport == "jupyter":
                closed.clear()
                async with aclosing(c.notebooks.exec_stream(
                    "fake", command="fake", transport=transport,
                )) as stream:
                    assert await anext(stream) == "one"
                assert closed.is_set()

    asyncio.run(run())
    assert threads and all(thread == threading.get_ident() for thread in threads)


def test_first_call_acquires_session_through_native_driver(client, tracked, monkeypatch):
    from inspire.platform.web.session.models import WebSession
    loads = []

    def cached(**kwargs):
        loads.append(threading.get_ident())
        return client._transport._session

    async def send(http, request, **kwargs):
        return httpx.Response(200, json={"ok": True}, request=request)

    def rows(self):
        assert self.client._transport.request("GET", "/fake")["ok"]
        return []

    monkeypatch.setattr(WebSession, "load", cached)
    monkeypatch.setattr(Workspaces, "_all", rows)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    from inspire.platform.web.offload import OffloadPool
    original_offload = OffloadPool.run

    async def preparation_only(self, function, *args, **kwargs):
        assert function.__name__ in {"prepare", "build"}, "authentication workflow offloaded"
        return await original_offload(self, function, *args, **kwargs)

    monkeypatch.setattr(OffloadPool, "run", preparation_only)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            c._client._transport._session = None
            await c.workspaces.list()
            assert c._client._transport._session is client._transport._session

    asyncio.run(run())
    assert loads and set(loads) == {threading.get_ident()}


@pytest.mark.parametrize("case", HANDLE_CASES)
def test_async_submission_handles_and_restored_refs(client, tracked, monkeypatch, case):
    from dataclasses import FrozenInstanceError, fields
    import json
    from unittest.mock import AsyncMock

    facade, producer, handle_name, ref_name, waiting, binding = case
    sync_type = getattr(inspire, handle_name)
    async_type = getattr(inspire, "Async" + handle_name)
    ref_type = getattr(inspire, ref_name)
    ref = ref_type("fake", "alpha", client.base_url, "fake-key", "ws-test")
    notebook = inspire.NotebookRef("nb", "alpha", client.base_url, "nb-key", "ws-test")
    extras = {"notebook": notebook} if producer == "save_image" else {"operation_id": "submission"}
    original = sync_type(name=ref.name, ref=ref, **extras)
    sync_facade = type(getattr(client, facade))
    monkeypatch.setattr(sync_facade, producer, lambda *a, **kw: original)
    snapshot = ref.to_dict()
    restored_ref = ref_type.from_dict(json.loads(json.dumps(snapshot)))
    assert restored_ref == ref and vars(restored_ref) == vars(ref)
    with pytest.raises(FrozenInstanceError):
        ref.key = "changed"

    async def run():
        async with InspireAsyncClient("alpha") as c:
            service = getattr(c, facade)
            if producer == "save_image":
                handle = await service.save_image(notebook, name="fake")
            elif producer == "register":
                handle = await service.register("fake", workspace="fake")
            else:
                handle = await service.create(object())
            assert type(handle) is async_type
            assert getattr(inspire.sdk, "Async" + handle_name) is async_type
            assert {f.name: getattr(handle, f.name) for f in fields(original)} == vars(original)
            assert "_facade" not in repr(handle)
            assert not hasattr(handle, "cancel")
            result = object()
            wait = AsyncMock(return_value=result)
            monkeypatch.setattr(service, waiting, wait)
            # Typed keyword arguments and defaults exactly match the facade.
            expected_sig = inspect.signature(getattr(type(service), waiting))
            handle_sig = inspect.signature(type(handle).wait)
            assert list(handle_sig.parameters) == [n for n in expected_sig.parameters if n != "ref"]
            for name, param in handle_sig.parameters.items():
                assert param.kind == expected_sig.parameters[name].kind
                assert param.default == expected_sig.parameters[name].default
            assert get_type_hints(type(handle).wait) == {
                k: v for k, v in get_type_hints(getattr(type(service), waiting)).items() if k != "ref"
            }
            assert await handle is result
            assert await handle is result
            assert wait.await_count == 2
            expected_arg = handle if producer == "save_image" else ref
            defaults = {n: p.default for n, p in expected_sig.parameters.items()
                        if p.kind == p.KEYWORD_ONLY}
            wait.assert_awaited_with(expected_arg, **defaults)
            options = dict(timeout=23, poll_interval=0.1)
            if "raise_on_failure" in defaults:
                options["raise_on_failure"] = True
            assert await handle.wait(**options) is result
            wait.assert_awaited_with(expected_arg, **(defaults | options))
            restored = await getattr(service, binding)(restored_ref, **(
                {"notebook": notebook} if binding == "bind_image_ref" else {}
            ))
            assert type(restored) is async_type and restored.ref is restored_ref
            assert await restored is result
            assert restored_ref.to_dict() == snapshot
            # Wrong account, type and origin fail locally, before polling.
            from dataclasses import replace
            for invalid in (replace(ref, account="beta"), replace(ref, base_url="https://invalid"), notebook
                            if ref_name != "NotebookRef" else inspire.JobRef(**vars(ref))):
                with pytest.raises(ValidationError):
                    await getattr(service, binding)(invalid, **(
                        {"notebook": notebook} if binding == "bind_image_ref" else {}
                    ))
            with pytest.raises(TypeError, match=r"client.jobs.wait.*InspireAsyncClient"):
                await original
            assert not hasattr(original, "future")
        with pytest.raises(ClientClosedError):
            await getattr(service, binding)(ref, **(
                {"notebook": notebook} if binding == "bind_image_ref" else {}
            ))

    asyncio.run(run())


def test_handles_gather_and_future_composition_are_concurrent(client, tracked, monkeypatch):
    from inspire.sdk.jobs import Jobs
    active = 0
    all_entered = asyncio.Event()

    async def poll(ref):
        nonlocal active
        active += 1
        if active == 3:
            all_entered.set()
        await asyncio.wait_for(all_entered.wait(), 1)
        return ref.key

    monkeypatch.setattr(Jobs, "wait", lambda self, ref, **kw: perform_sync(call(poll, ref)))

    async def run():
        nonlocal active
        async with InspireAsyncClient("alpha") as c:
            handles = [await c.jobs.bind_ref(inspire.JobRef(str(i), "alpha", client.base_url, str(i)))
                       for i in range(3)]
            for mode in ("gather", "wait", "as_completed"):
                active = 0
                all_entered.clear()
                if mode == "gather":
                    results = await asyncio.gather(*handles)
                else:
                    futures = [h.future() for h in handles]
                    assert all(isinstance(f, asyncio.Future) for f in futures)
                    if mode == "wait":
                        done, pending = await asyncio.wait(futures, timeout=2)
                        assert not pending
                        results = [f.result() for f in done]
                    else:
                        results = [await f for f in asyncio.as_completed(futures)]
                assert sorted(results) == ["0", "1", "2"] and active == 3
            # Each future is a new wait, even for the same handle.
            a, b, d = (handles[0].future() for _ in range(3))
            assert a is not b and b is not d
            assert await asyncio.gather(a, b, d) == ["0"] * 3

    asyncio.run(run())


@pytest.mark.parametrize("case", HANDLE_CASES)
def test_handle_cancellation_interrupts_polling_without_stop(client, tracked, monkeypatch, case):
    import time
    facade, _, _, ref_name, waiting, binding = case
    service_type = type(getattr(client, facade))
    polled = asyncio.Event()
    polls = []

    def get(self, *a, **kw):
        polls.append(1)
        polled.set()
        return types.SimpleNamespace(status="PENDING")

    if waiting == "wait":
        monkeypatch.setattr(service_type, "_resolve", lambda self, ref, ws: ref)
        monkeypatch.setattr(service_type, "get", get)
        monkeypatch.setattr(service_type, "stop", lambda *a, **kw: pytest.fail("cancel stopped workload"))
    else:
        def image_poll(**kwargs):
            get(None)
            perform_sync(call(time.sleep, 60))
            pytest.fail("polling was not cancelled")
        monkeypatch.setattr("inspire.platform.web.browser_api.wait_for_image_ready", image_poll)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            ref = getattr(inspire, ref_name)("fake", "alpha", client.base_url, "key", "ws-test")
            extra = {"notebook": inspire.NotebookRef(**vars(ref))} if binding == "bind_image_ref" else {}
            handle = await getattr(getattr(c, facade), binding)(ref, **extra)
            for mode in ("await", "wait", "future"):
                polled.clear()
                async def direct():
                    return await handle
                task = (handle.future() if mode == "future" else asyncio.create_task(
                    direct() if mode == "await" else handle.wait(poll_interval=60)
                ))
                await asyncio.wait_for(polled.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                assert not c._active
            assert polls == [1, 1, 1]
            assert (await c.cache.stats())["entries"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("case", HANDLE_CASES[:6])
def test_failed_workload_handles_follow_facade_policy(client, tracked, monkeypatch, case):
    facade, _, _, ref_name, _, _ = case
    cls = type(getattr(client, facade))
    failed = types.SimpleNamespace(name="fake", status="FAILED")
    monkeypatch.setattr(cls, "_resolve", lambda self, ref, ws: ref)
    monkeypatch.setattr(cls, "get", lambda *a, **kw: failed)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            handle = await getattr(c, facade).bind_ref(
                getattr(inspire, ref_name)("fake", "alpha", client.base_url, "key")
            )
            assert await handle is failed
            assert await handle is failed
            with pytest.raises(inspire.InspireError):
                await handle.wait(raise_on_failure=True)

    asyncio.run(run())


def test_saved_image_without_identity_and_models_without_wait(client, tracked, monkeypatch):
    from inspire.sdk.notebooks import Notebooks
    Models = type(client.models)
    notebook = inspire.NotebookRef("fake", "alpha", client.base_url, "key", "ws-test")
    missing = inspire.ImageSaveHandle("image", None, notebook, warning="not visible yet")
    model = inspire.ModelRegisterHandle("model", inspire.ModelRef(**vars(notebook)), "op")
    monkeypatch.setattr(Notebooks, "save_image", lambda *a, **kw: missing)
    monkeypatch.setattr(Models, "register", lambda *a, **kw: model)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            handle = await c.notebooks.save_image(notebook, name="image")
            assert handle.warning == missing.warning and handle.ref is None
            with pytest.raises(ValidationError, match="Image identity is not available"):
                await handle
            result = await c.models.register("model", source_path="/fake", workspace="fake", project="fake")
            assert result is model and not inspect.isawaitable(result)
            assert not hasattr(result, "future") and not hasattr(c.models, "bind_ref")

    asyncio.run(run())
