"""Phase Z: real local files/processes, synthetic browser and HTTP, no platform."""

from __future__ import annotations

import asyncio
import builtins
from contextlib import contextmanager
import io
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from inspire.bridge.tunnel import ssh_exec, scp
from inspire.platform.web.flow import call, run_sync
from inspire.platform.web.transport import Transport
from inspire.platform.web.transport_async import AsyncDriver
from inspire.platform.web.session import auth
from inspire.platform.web.session.models import WebSession
from inspire.sdk.cache import CatalogCache
from inspire.sdk.catalog_store import CatalogStore
from inspire.services.execution import remote_exec, notebook_transfer
from inspire.sdk import notebook_transfer as sdk_transfer

pytestmark = pytest.mark.timeout(15, method="thread")


def owner():
    transport = Transport("test", "https://example.invalid", username="fake")
    transport._session = WebSession(
        storage_state={"cookies": [{"name": "x", "value": "fake"}]},
        created_at=time.time(),
        workspace_id="ws",
        account="test",
        base_url=transport.base_url,
    )
    return transport


class Ticker:
    """A slow leaf can finish only after this loop has actually progressed."""

    def __init__(self):
        self.count = 0
        self.waiters = []
        self.observed = []

    async def run(self):
        while True:
            self.count += 1
            for count, ready in list(self.waiters):
                if self.count >= count + 5:
                    ready.set()
                    self.waiters.remove((count, ready))
            await asyncio.sleep(0.001)

    def slow(self, label):
        before = self.count
        ready = threading.Event()
        self.loop.call_soon_threadsafe(self.waiters.append, (before, ready))
        ready.wait(0.3)
        self.observed.append((label, self.count - before))

    async def check(self, label, operation):
        before = self.count
        result = await operation
        assert self.count - before >= 5, f"{label} blocked the event loop"
        return result


@pytest.fixture
def local_ssh(monkeypatch):
    bridge = SimpleNamespace(name="fake", ssh_user="fake", ssh_port=22)
    monkeypatch.setattr(remote_exec, "load_tunnel_config", lambda **kw: None)
    monkeypatch.setattr(notebook_transfer, "load_tunnel_config", lambda **kw: None)
    monkeypatch.setattr(
        ssh_exec, "_resolve_bridge_and_proxy", lambda *a, **kw: (None, bridge, "unused")
    )
    monkeypatch.setattr(scp, "_resolve_bridge_and_proxy", lambda *a, **kw: (None, bridge, "unused"))
    monkeypatch.setattr(ssh_exec, "build_ssh_process_env", lambda: None)
    monkeypatch.setattr(scp, "build_ssh_process_env", lambda: None)
    # Allow five ticker steps even with the coarser Windows event-loop clock.
    # A local Python process supplies output; SSH is never launched.
    monkeypatch.setattr(
        ssh_exec,
        "_build_ssh_base_args",
        lambda **kw: [
            sys.executable,
            "-c",
            "import sys,time; sys.stdout.write('one'); sys.stdout.flush(); "
            "time.sleep(.2); sys.stderr.write('two')",
        ],
    )


def test_fixed_paths_keep_event_loop_running(tmp_path, monkeypatch, local_ssh):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ticker = Ticker()
    source = tmp_path / "source"
    source.write_bytes(b"binary\x00payload")
    original_read = Path.read_text
    original_write = CatalogStore._write
    original_upload = Path.open
    original_builtin_open = builtins.open

    def session_file(path, *a, **kw):
        if "web_session" in str(path):
            ticker.slow("session_file")
        return original_builtin_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", session_file)

    # Session publication now writes through an already-private descriptor;
    # delaying builtins.open alone no longer exercises the write-side leaf.
    from inspire.platform.web.session import models as session_models

    original_session_write = session_models.atomic_write_text

    def session_write(*a, **kw):
        ticker.slow("session_write")
        return original_session_write(*a, **kw)

    monkeypatch.setattr(session_models, "atomic_write_text", session_write)

    def read(path, *a, **kw):
        ticker.slow("read_text")
        return original_read(path, *a, **kw)

    def write(store, data):
        ticker.slow("catalog_write")
        return original_write(store, data)

    def open_file(path, *a, **kw):
        if path == source:
            ticker.slow("upload_read")
        return original_upload(path, *a, **kw)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(CatalogStore, "_write", write)
    # Every catalog lock acquisition includes an intentionally slow local wait.
    from inspire.sdk import catalog_store

    original_lock = catalog_store.exclusive_cache_lock

    @contextmanager
    def lock(*a, **kw):
        ticker.slow("catalog_lock")
        with original_lock(*a, **kw):
            yield

    monkeypatch.setattr(catalog_store, "exclusive_cache_lock", lock)

    class Writer(io.StringIO):
        def write(self, text):
            ticker.slow("output_write")
            return super().write(text)

    async def run():
        ticker.loop = asyncio.get_running_loop()
        pulse = asyncio.create_task(ticker.run())
        transport = owner()
        store = CatalogStore("test", transport.base_url)
        cache = CatalogCache(store=store)
        key = ("current_user", "test", transport.base_url)
        try:
            async with AsyncDriver(transport) as driver:

                async def invoke(fn):
                    return await run_sync(driver, fn)

                for label, fn in [
                    ("catalog miss", lambda: cache._get(key, lambda: {"id": "user"})),
                    ("catalog hit", lambda: cache._get(key, lambda: pytest.fail("missed cache"))),
                    ("catalog invalidation", cache.clear),
                    ("upload", lambda: sdk_transfer._read_upload(source, 100)),
                    ("session save", lambda: auth._persist(transport._session, account="test")),
                    ("session load", lambda: WebSession.load(account="test")),
                ]:
                    result = await ticker.check(label, invoke(fn))
                    if label == "upload":
                        assert result == b"binary\x00payload"
                writer = Writer()
                chunks = []

                async def callback(chunk):
                    await asyncio.sleep(0.01)
                    chunks.append(chunk)

                result = await ticker.check(
                    "ssh exec",
                    driver.execute(
                        call(
                            remote_exec.exec_in_notebook_ssh,
                            bridge_name="fake",
                            account="test",
                            command="printf one; sleep .05; printf two >&2",
                            timeout=2,
                            output_to=writer,
                            on_output=callback,
                        )
                    ),
                )
                assert (result.stdout, result.stderr, result.completed) == ("one", "two", True)
                assert "".join(chunks) == writer.getvalue() == "onetwo"
                # SCP uses the native subprocess backend with the same shared argument builder.
                monkeypatch.setattr(
                    scp,
                    "_build_scp_base_args",
                    lambda **kw: [
                        sys.executable,
                        "-c",
                        "import sys,time; time.sleep(.2); sys.stdout.buffer.write(b'copied\\n')",
                    ],
                )
                copied = await ticker.check(
                    "scp",
                    driver.execute(
                        call(
                            scp.run_scp_transfer,
                            str(source),
                            "/synthetic",
                            bridge_name="fake",
                        )
                    ),
                )
                assert copied.returncode == 0 and copied.stdout == "copied\n"
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            transport.close()
        assert ticker.observed
        assert all(count >= 5 for _, count in ticker.observed), ticker.observed

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["timeout", "cancel", "callback"])
def test_native_ssh_reaps_process_on_every_exit(monkeypatch, local_ssh, mode):
    processes = []
    original_spawn = asyncio.create_subprocess_exec
    monkeypatch.setattr(
        ssh_exec,
        "_build_ssh_base_args",
        lambda **kw: [
            sys.executable,
            "-u",
            "-c",
            "import time; print('ready'); time.sleep(30)",
        ],
    )

    async def spawn(*a, **kw):
        process = await original_spawn(*a, **kw)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def run():
        ready = asyncio.Event()

        async def callback(chunk):
            ready.set()
            if mode == "callback":
                raise subprocess.TimeoutExpired("user callback", 7)

        transport = owner()
        async with AsyncDriver(transport) as driver:
            task = asyncio.create_task(
                driver.execute(
                    call(
                        remote_exec.exec_in_notebook_ssh,
                        bridge_name="fake",
                        account="test",
                        command="unused",
                        timeout=0.1 if mode == "timeout" else 5,
                        on_output=callback,
                    )
                )
            )
            if mode == "cancel":
                await ready.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif mode == "callback":
                with pytest.raises(subprocess.TimeoutExpired, match="user callback"):
                    await task
            else:
                result = await task
                assert result.returncode == 124 and not result.completed
                assert "ready" in result.stdout
        assert processes and all(p.returncode is not None for p in processes)
        transport.close()

    asyncio.run(run())


def test_request_preparation_warms_netrc_and_certificates(tmp_path, monkeypatch):
    import ssl
    from inspire.platform.web.session import proxy

    ticker = Ticker()
    calls = []
    original = ssl.create_default_context
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def netrc(url, **kw):
        ticker.slow("netrc")
        calls.append("netrc")
        return ("synthetic", "unused")

    def context(*a, **kw):
        ticker.slow("certificates")
        calls.append("tls")
        return original(*a, **kw)

    def config(*a, **kw):
        ticker.slow("config")
        return "", {}

    monkeypatch.setattr(ssl, "create_default_context", context)
    monkeypatch.setattr("requests.utils.get_netrc_auth", netrc)
    monkeypatch.setattr(proxy, "_load_proxy_toml_values", config)

    async def send(self, request, **kw):
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)

    async def run():
        ticker.loop = asyncio.get_running_loop()
        pulse = asyncio.create_task(ticker.run())
        transport = owner()
        try:
            for _ in range(3):
                assert await ticker.check(
                    "preparation", transport.request_async("GET", "/test")
                ) == {"ok": True}
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            transport.close()
        assert calls.count("netrc") == calls.count("tls") == 1
        assert all(count >= 5 for _, count in ticker.observed), ticker.observed

    asyncio.run(run())


@pytest.mark.parametrize("rejected", [False, True])
@pytest.mark.parametrize("verification", [False, True])
def test_browser_login_same_sequence_messages_and_loop_progress(monkeypatch, rejected, verification):
    from playwright import async_api, sync_api

    traces = []
    ticker = Ticker()
    monkeypatch.setattr(auth, "resolve_playwright_proxy_config", lambda **kw: (None, "none"))
    monkeypatch.setattr(auth, "describe_effective_proxy_config", lambda **kw: {})
    monkeypatch.setattr(auth, "_persist", lambda *a, **kw: None)

    def requests_requires_code(*args, **kwargs):
        raise auth._CasVerificationRequired("requests needs a verification code")

    monkeypatch.setattr(auth, "_login_with_cas_requests", requests_requires_code)

    def runtime(asynchronous):
        trace = []
        traces.append(trace)

        class Node:
            url = "https://example.invalid/login"
            status = 401 if rejected else 200

            @property
            def chromium(self):
                return self

            @property
            def request(self):
                return self

            @property
            def first(self):
                return self

            def locator(self, value):
                return self

            def __getattr__(self, name):
                def invoke(*args, **kwargs):
                    trace.append((name, args, kwargs))
                    if name == "wait_for_timeout" and rejected and args == (500,):
                        # The shared deadline advances without spending 30 real seconds.
                        clock[0] += 31
                    if name == "json":
                        return {"Result": {"id": "synthetic"}}
                    if name == "storage_state":
                        return {"cookies": []}
                    if name == "cookies":
                        return []
                    if name == "content" and verification:
                        return (
                            '<form id="fm1"><input name="username"><input name="password">'
                            '<input name="authcode"><img src="/cas/captcha.jpg"></form>'
                        )
                    if name == "content":
                        return '<div class="form-error">rejected</div>'
                    return self

                if not asynchronous:
                    return invoke

                async def execute(*args, **kwargs):
                    before = ticker.count
                    await asyncio.sleep(0.01)
                    assert ticker.count > before
                    return invoke(*args, **kwargs)

                return execute

        node = Node()

        class Context:
            def __enter__(self):
                return node

            def __exit__(self, *args):
                trace.append(("exit",))

        class AsyncContext:
            async def __aenter__(self):
                return node

            async def __aexit__(self, *args):
                trace.append(("exit",))

        return AsyncContext() if asynchronous else Context()

    clock = [time.time()]
    monkeypatch.setattr(auth.time, "time", lambda: clock[0])
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: runtime(False))
    monkeypatch.setattr(async_api, "async_playwright", lambda: runtime(True))
    options = dict(base_url="https://example.invalid", headless=True, account="test")

    def sync():
        try:
            return auth._submit_credentials("fake", "unused", **options)
        except Exception as error:
            return type(error), str(error), getattr(error, "credential_rejection", None)

    expected = sync()

    async def run():
        ticker.loop = asyncio.get_running_loop()
        pulse = asyncio.create_task(ticker.run())
        transport = owner()
        try:
            async with AsyncDriver(transport) as driver:
                try:
                    actual = await driver.execute(
                        call(auth._submit_credentials, "fake", "unused", **options)
                    )
                except Exception as error:
                    actual = type(error), str(error), getattr(error, "credential_rejection", None)
            if rejected or verification:
                assert actual == expected
            else:
                assert actual.storage_state == expected.storage_state
                assert actual.user_detail == expected.user_detail
            assert traces[0] == traces[1]
            for trace in traces:
                actions = [event[0] for event in trace]
                assert actions.count("fill") == (0 if verification else 2)
                assert actions.count("press") == (0 if verification else 1)
                if verification:
                    assert actual[0] is auth._CasVerificationRequired
                    assert actions.count("close") == 1
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            transport.close()

    asyncio.run(run())


@pytest.mark.skipif(
    os.name != "posix",
    reason="The Linux notebook helper runs locally through /bin/bash and stages files in /tmp.",
)
@pytest.mark.parametrize("download", [False, True])
def test_shared_ssh_transfer_uses_async_processes_and_publishes(
    tmp_path, monkeypatch, local_ssh, download
):
    monkeypatch.setattr(
        ssh_exec, "_build_ssh_base_args",
        lambda **kw: [
            sys.executable, "-c",
            "import subprocess,sys; sys.exit(subprocess.run(['/bin/bash','-s'],"
            "input=sys.stdin.buffer.read()).returncode)",
        ],
    )
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    source, target = (remote, local) if download else (local, remote)
    source.write_bytes(b"complete\x00file")
    monkeypatch.setattr(
        scp,
        "_build_scp_base_args",
        lambda **kw: [
            sys.executable,
            "-c",
            "import shutil,sys,time; time.sleep(.03); "
            "a,b=[p.split('@localhost:',1)[-1] for p in sys.argv[1:]]; shutil.copyfile(a,b)",
        ],
    )
    ticker = Ticker()
    original_mkdtemp = notebook_transfer.tempfile.mkdtemp

    def mkdtemp(*a, **kw):
        ticker.slow("temporary_directory")
        return original_mkdtemp(*a, **kw)

    monkeypatch.setattr(notebook_transfer.tempfile, "mkdtemp", mkdtemp)

    async def run():
        ticker.loop = asyncio.get_running_loop()
        pulse = asyncio.create_task(ticker.run())
        transport = owner()
        try:
            async with AsyncDriver(transport) as driver:
                result = await ticker.check(
                    "transfer",
                    driver.execute(
                        call(
                            notebook_transfer.transfer_ssh,
                            local=str(local),
                            remote=str(remote),
                            download=download,
                            recursive=False,
                            overwrite=False,
                            bridge_name="fake",
                            account="test",
                            timeout=5,
                        )
                    ),
                )
            assert result.bytes_transferred == len(b"complete\x00file")
            assert result.files_transferred == 1 and result.transport == "ssh"
            assert target.read_bytes() == b"complete\x00file"
            assert ticker.observed and all(n >= 5 for _, n in ticker.observed)
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            transport.close()

    asyncio.run(run())


def test_authentication_lock_and_guard_steps_do_not_block(tmp_path, monkeypatch):
    from inspire.platform.web.async_context import authentication_context
    from inspire.platform.web.session import login_guard
    from inspire.accounts import cache_lock

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ticker = Ticker()
    fingerprint = login_guard.credential_fingerprint
    try_acquire = cache_lock._try_acquire
    attempts = []

    def slow_fingerprint(*args):
        ticker.slow("fingerprint")
        return fingerprint(*args)

    def contend(descriptor):
        ticker.slow("lock_attempt")
        attempts.append(descriptor)
        return False if len(attempts) < 3 else try_acquire(descriptor)

    monkeypatch.setattr(login_guard, "credential_fingerprint", slow_fingerprint)
    monkeypatch.setattr(cache_lock, "_try_acquire", contend)

    async def run():
        ticker.loop = asyncio.get_running_loop()
        pulse = asyncio.create_task(ticker.run())
        transport = owner()
        context = login_guard.guarded_credential_submission("fake", "unused", account="test")
        try:
            async with authentication_context(context, transport):
                assert ticker.count >= 5
            assert len(attempts) == 3
            assert all(n >= 5 for _, n in ticker.observed), ticker.observed
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            transport.close()

    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("transport_kind", ["ssh", "jupyter"])
def test_public_async_output_callback(tmp_path, monkeypatch, local_ssh, stream, transport_kind):
    from inspire.sdk import InspireAsyncClient
    from inspire.sdk.notebooks import Notebooks
    from inspire.sdk import _async_runtime
    from inspire.services.execution.async_output import deliver_output

    monkeypatch.setattr(
        ssh_exec, "_build_ssh_base_args",
        lambda **kw: [
            sys.executable, "-c",
            "import sys,time; sys.stdout.write('one'); sys.stdout.flush(); "
            "time.sleep(.03); sys.stdout.write('two')",
        ],
    )
    base = owner()
    # Use the real public facade/runtime with an isolated account and cached session.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    directory = tmp_path / ".inspire" / "accounts" / "test"
    directory.mkdir(parents=True)
    (directory / "config.toml").write_text('[auth]\nusername="fake"\npassword="unused"\n')
    factory = _async_runtime.InspireClient

    def create(**options):
        result = factory(**options)
        result._transport._session = base._session
        return result

    monkeypatch.setattr(_async_runtime, "InspireClient", create)
    monkeypatch.setattr(
        Notebooks,
        "_resolve",
        lambda *a: SimpleNamespace(key="fake", workspace_id="ws", name="fake"),
    )
    monkeypatch.setattr(remote_exec, "cached_notebook_bridge", lambda **kw: "fake")

    async def jupyter(*, on_output, **kw):
        for chunk in ("one", "two"):
            await deliver_output(on_output, chunk)
        return remote_exec.ExecResult(0, "onetwo", "onetwo", "", True, "jupyter")

    monkeypatch.setattr(remote_exec, "exec_in_notebook_jupyter_async", jupyter)
    chunks, threads = [], []

    async def callback(chunk):
        await asyncio.sleep(0.01)
        chunks.append(chunk)
        threads.append(threading.get_ident())

    async def run():
        async with InspireAsyncClient("test", base_url=base.base_url) as client:
            method = client.notebooks.exec_stream if stream else client.notebooks.exec
            operation = method(
                "fake",
                command="printf one; sleep .03; printf two",
                transport=transport_kind,
                on_output=callback,
            )
            if stream:
                received = [chunk async for chunk in operation]
                assert received == chunks
            else:
                result = await operation
                assert result.stdout == "onetwo"
        assert "".join(chunks) == "onetwo"
        assert all(thread == threading.get_ident() for thread in threads)

    asyncio.run(run())


@pytest.mark.parametrize("limit,capture", [(128, True), (None, True), (128, False)])
def test_native_ssh_capture_matches_sync(local_ssh, monkeypatch, limit, capture):
    import shlex

    command = shlex.join(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('汉字' * 6000); sys.stderr.write('error' * 1000)",
        ]
    )
    monkeypatch.setattr(
        ssh_exec, "_build_ssh_base_args",
        lambda **kw: [
            sys.executable, "-c",
            "import sys; sys.stdout.buffer.write(('汉字' * 6000).encode('utf-8')); "
            "sys.stderr.buffer.write(b'error' * 1000)",
        ],
    )
    options = dict(
        bridge_name="fake",
        account="test",
        command=command,
        timeout=5,
        max_output_bytes=limit,
        capture=capture,
    )
    expected = remote_exec.exec_in_notebook_ssh(**options)
    assert expected.returncode == 0 and expected.completed
    assert expected.total_output_bytes == len(("汉字" * 6000 + "error" * 1000).encode("utf-8"))

    async def run():
        transport = owner()
        try:
            async with AsyncDriver(transport) as driver:
                result = await driver.execute(call(remote_exec.exec_in_notebook_ssh, **options))
            assert result == expected
            assert result.total_output_bytes == expected.total_output_bytes
        finally:
            transport.close()

    asyncio.run(run())


def test_shared_index_io_keeps_event_loop_running(tmp_path, monkeypatch):
    from inspire.services.catalog.resource_index import ResourceIndex, ResourceIdentity, ResourceScope
    ticker = Ticker()
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = ResourceScope("https://example.invalid", "user", "job")
    original = index._connect

    @contextmanager
    def slow_connect():
        ticker.slow("sqlite_connect")
        with original() as connection:
            yield connection

    monkeypatch.setattr(index, "_connect", slow_connect)
    async def run():
        ticker.loop = asyncio.get_running_loop()
        pulse = asyncio.create_task(ticker.run())
        transport = owner()
        try:
            async with AsyncDriver(transport) as driver:
                def refresh():
                    with index.refresh_lease(scope) as acquired:
                        assert acquired
                        index.reconcile(scope, [ResourceIdentity("id", "name")])
                    assert index.lookup(scope, "name")[0].resource_id == "id"
                await ticker.check("shared index", run_sync(driver, refresh))
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            transport.close()
    asyncio.run(run())


def test_a_pipe_torn_down_mid_drain_is_the_end_of_the_stream():
    """A child exiting while its output is read must not raise at the caller.

    Under load the child can close its pipe between two reads, and asyncio
    surfaces that as ConnectionResetError. It reached the caller as a bare OSError
    outside the InspireError hierarchy, and only ever showed up as a rare red
    build. The exit status, not the pipe, decides whether the command worked.
    """
    import asyncio
    from inspire.bridge.tunnel import process_async

    chunks: list[str] = []

    class TornStream:
        def __init__(self):
            self.reads = 0

        async def read(self, _size):
            self.reads += 1
            if self.reads == 1:
                return b"partial output"
            raise ConnectionResetError("Connection lost")

    async def run():
        process = SimpleNamespace(
            stdout=TornStream(), stderr=None, stdin=None,
            returncode=0, wait=_immediate_zero, kill=lambda: None,
        )

        async def spawn(*args, **kwargs):
            return process

        original = process_async._spawn
        process_async._spawn = spawn
        try:
            return await process_async.stream_process(
                ["irrelevant"], None, chunks.append, None, None, None, "bridge",
                deliver=_deliver_now,
            )
        finally:
            process_async._spawn = original

    assert asyncio.run(run()) == 0
    assert "".join(chunks) == "partial output"


async def _immediate_zero():
    return 0


async def _deliver_now(callback, chunk):
    callback(chunk)
