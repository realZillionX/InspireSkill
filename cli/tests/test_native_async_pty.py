"""Offline equivalence tests for both wire and execution adapters."""
import asyncio
import json
import struct
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from inspire.platform.web import pty_socket as wire
from inspire.platform.web.session import WebSession
from inspire.services.execution import remote_exec as remote
from inspire.platform.web.browser_api import jupyter_terminal as terminal


def frame(opcode, payload=b"", *, final=True, masked=False):
    first = opcode | (128 if final else 0)
    size = len(payload)
    second = 128 if masked else 0
    if size < 126:
        header = bytes([first, second | size])
    elif size <= 65535:
        header = bytes([first, second | 126]) + struct.pack("!H", size)
    else:
        header = bytes([first, second | 127]) + struct.pack("!Q", size)
    mask = b"abcd" if masked else b""
    return header + mask + (bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if mask else payload)


@pytest.mark.parametrize("masked", [False, True])
@pytest.mark.parametrize("size", [0, 125, 126, 65536])
def test_same_wire_bytes_produce_same_frames(masked, size):
    payload = b"x" * size
    data = (frame(1, b"start", final=False, masked=masked)
            + frame(9, b"ping") + frame(0, payload, masked=masked)
            + frame(10, b"pong") + frame(8, b"\x03\xe8"))
    sync = wire.WebSocketClient("ws://example.test", {})
    sync.sock = SimpleNamespace(recv=lambda size: b"")
    sync._recv_buffer = data
    expected = [sync.recv_frame() for _ in range(4)]

    async def run():
        client = wire.AsyncWebSocketClient("ws://example.test", {})
        client.reader = asyncio.StreamReader()
        # Same bytes arrive in short arbitrary chunks.
        for start in range(0, len(data), 7):
            client.reader.feed_data(data[start:start + 7])
        client.reader.feed_eof()
        assert [await client.recv_frame() for _ in range(4)] == expected
        assert client._protocol.close_payload == sync._protocol.close_payload == b"\x03\xe8"
    asyncio.run(run())
    assert expected == [(9, b"ping"), (1, b"start" + payload), (10, b"pong"), (8, b"\x03\xe8")]


@pytest.mark.parametrize("data", [frame(0, b"bad"), frame(9, b"bad", final=False),
                                  frame(8, b"x"), bytes([0xF1, 0]),
                                  frame(1, b"a", final=False) + frame(2, b"b")])
def test_protocol_errors_match(data):
    sync = wire.WebSocketClient("ws://example.test", {})
    sync.sock = SimpleNamespace(recv=lambda size: b"")
    sync._recv_buffer = data
    with pytest.raises(wire.JobShellError) as error:
        sync.recv_frame()

    async def run():
        client = wire.AsyncWebSocketClient("ws://example.test", {})
        client.reader = asyncio.StreamReader()
        client.reader.feed_data(data)
        client.reader.feed_eof()
        with pytest.raises(wire.JobShellError, match=str(error.value)):
            await client.recv_frame()
    asyncio.run(run())


def session():
    return WebSession(storage_state={"cookies": [{"name": "inspire-session", "value": "fake"}]},
                      created_at=1, base_url="https://example.test")


@pytest.mark.parametrize("jupyter", [False, True])
@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("capture", [False, True])
def test_exec_results_match(monkeypatch, jupyter, timeout, capture):
    texts = ["$ ", "z" * 200000, "\nDONE", ":exit:7\n"]
    if timeout:
        texts = texts[:2]
    frames = [(9, b"ping")] + [(1, json.dumps(["stdout", text]).encode() if jupyter else text.encode()) for text in texts]
    sent = []

    class SyncSocket:
        def __init__(self, *args, **kwargs):
            self.frames = iter(frames)
        def connect(self):
            pass
        def close(self):
            sent.append("close")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
        def fileno(self):
            return 0
        def set_read_timeout(self, value):
            pass
        def has_pending_data(self):
            return True
        def recv_frame(self):
            try:
                return next(self.frames)
            except StopIteration:
                raise TimeoutError("scripted timeout")
        def send_text(self, text):
            sent.append(text)
        def send_pong(self, payload):
            sent.append(payload)
        def _send_frame(self, opcode, payload):
            self.send_pong(payload)

    class AsyncSocket(SyncSocket):
        async def connect(self):
            pass
        async def close(self):
            super().close()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            await self.close()
        async def recv_frame(self):
            await asyncio.sleep(0)
            return super().recv_frame()
        async def send_text(self, text):
            super().send_text(text)
        async def send_pong(self, payload):
            super().send_pong(payload)

    monkeypatch.setattr(remote.select, "select", lambda *args: ([0], [], []))
    monkeypatch.setattr(remote, "WebSocketClient", SyncSocket)
    monkeypatch.setattr(remote, "AsyncWebSocketClient", AsyncSocket)
    monkeypatch.setattr(wire, "WebSocketClient", SyncSocket)
    monkeypatch.setattr(wire, "AsyncWebSocketClient", AsyncSocket)
    options = dict(session=session(), marker="DONE", max_output_bytes=100, capture=capture)
    if jupyter:
        options.update(ws_url="ws://example.test", stdin_data="command", timeout_ms=1000)
        def capture_sync(**kwargs):
            return terminal._capture_terminal_output(**options)
        async def capture_async(**kwargs):
            return await terminal._capture_terminal_output_async(**options)
        monkeypatch.setattr(remote, "run_command_capture_in_notebook", capture_sync)
        monkeypatch.setattr(remote, "run_command_capture_in_notebook_async", capture_async)
        service_options = dict(session=session(), notebook_id="fake", command="command",
                               timeout=1, marker="DONE", max_output_bytes=100, capture=capture)
        result = remote.exec_in_notebook_jupyter(**service_options)
        other = asyncio.run(remote.exec_in_notebook_jupyter_async(**service_options))
    else:
        options.update(url="ws://example.test", command="command", timeout=1)
        result = remote.exec_over_pty_websocket(**options)
        other = asyncio.run(remote.exec_over_pty_websocket_async(**options))
    assert asdict(result) == asdict(other)
    assert result.completed is not timeout
    assert result.returncode == (124 if timeout else 7)
    assert result.truncated is capture
    assert result.total_output_bytes >= 200000
    assert sent.count("close") == 2


def test_native_stream_handshake_ping_and_close(monkeypatch):
    import base64
    import hashlib
    monkeypatch.setattr(wire.WebSocketClient, "_proxy_url", lambda _: "")
    events = []

    async def run():
        async def server(reader, writer):
            request = (await reader.readuntil(b"\r\n\r\n")).decode()
            key = wire.WebSocketClient._header_value(request, "Sec-WebSocket-Key")
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(), usedforsecurity=False
            ).digest()).decode()
            writer.write(("HTTP/1.1 101 Switching Protocols\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode()
                         + frame(9, b"hello"))
            await writer.drain()
            protocol = wire._WebSocketProtocol()
            for _ in range(2):
                program = protocol.frame()
                try:
                    size = next(program)
                    while True:
                        size = program.send(await reader.readexactly(size))
                except StopIteration as done:
                    events.append(done.value)
                if len(events) == 1:
                    writer.write(frame(8, b"\x03\xe8"))
                    await writer.drain()
            writer.close()
            await writer.wait_closed()

        listener = await asyncio.start_server(server, "127.0.0.1", 0)
        async with listener:
            port = listener.sockets[0].getsockname()[1]
            async with wire.AsyncWebSocketClient(f"ws://127.0.0.1:{port}/pty?q=1", {}) as client:
                assert await client.recv_frame() == (9, b"hello")
                await client.send_pong(b"hello")
                assert await client.recv_frame() == (8, b"\x03\xe8")
            for _ in range(100):
                if len(events) == 2:
                    break
                await asyncio.sleep(0.001)
        assert events == [(10, b"hello"), (8, b"\x03\xe8")]
    asyncio.run(run())


@pytest.mark.parametrize("status", [200, 302, 401, 403, 429, 503])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE", "PATCH"])
def test_browser_clients_share_requests_and_classification(status, method):
    from inspire.platform.web.session.browser_client import _BrowserRequestClient, AsyncBrowserRequestClient
    events = []

    class Response:
        headers = {"Retry-After": "2"}
        def text(self):
            return "failure"
        def json(self):
            return {"ok": True}
    Response.status = status

    class AsyncResponse(Response):
        async def text(self):
            return super().text()
        async def json(self):
            return super().json()

    def send(url, **options):
        events.append((url, options))
        return Response()
    async def async_send(url, **options):
        events.append((url, options))
        return AsyncResponse()

    sync = object.__new__(_BrowserRequestClient)
    sync._closed = False
    sync._context = SimpleNamespace(request=SimpleNamespace(get=send, post=send, delete=send))
    asynchronous = AsyncBrowserRequestClient(session())
    asynchronous._context = SimpleNamespace(request=SimpleNamespace(get=async_send, post=async_send, delete=async_send))

    def outcome(fn):
        try:
            return fn()
        except Exception as error:
            return type(error), str(error), getattr(error, "retry_after", None)
    options = dict(body={"x": 1}, headers={"Referer": "test"}, timeout=3)
    assert outcome(lambda: sync.request_json(method, "https://example.test", **options)) == outcome(
        lambda: asyncio.run(asynchronous.request_json(method, "https://example.test", **options)))
    if method != "PATCH":
        assert events[0] == events[1]
        assert events[0][1]["max_redirects"] == 0


@pytest.mark.parametrize("stage", ["launch", "context", "request"])
def test_async_browser_failure_closes_created_resources(monkeypatch, stage):
    from inspire.platform.web.session.browser_client import AsyncBrowserRequestClient
    from playwright import async_api
    events = []

    async def fail():
        raise RuntimeError("scripted")

    class Resource:
        async def start(self):
            events.append("start")
            return self
        @property
        def chromium(self):
            return self
        async def launch(self, **kwargs):
            events.append("launch")
            if stage == "launch":
                await fail()
            return self
        async def new_context(self, **kwargs):
            events.append("context")
            if stage == "context":
                await fail()
            return self
        async def close(self):
            events.append("close")
        async def stop(self):
            events.append("stop")

    monkeypatch.setattr(async_api, "async_playwright", Resource)
    async def run():
        with pytest.raises(RuntimeError, match="scripted"):
            async with AsyncBrowserRequestClient(session()):
                await fail()
    asyncio.run(run())
    assert events[-1] == "stop"
    assert events.count("close") == {"launch": 0, "context": 1, "request": 2}[stage]


@pytest.mark.parametrize("cli_compat", [False, True])
def test_async_browser_runtime_hint(monkeypatch, cli_compat):
    from inspire.platform.web.transport import Transport
    from inspire.platform.web.transport_async import AsyncDriver
    from inspire.platform.web.transport_core import Send
    from inspire.platform.web.session import browser_client

    class MissingBrowser:
        def __init__(self, session):
            pass
        async def __aenter__(self):
            raise RuntimeError("BrowserType.launch: Executable doesn't exist at /missing/chromium")
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(browser_client, "AsyncBrowserRequestClient", MissingBrowser)
    owner = Transport(None, "https://example.test", username="", allow_browser=True, cli_compat=cli_compat)
    owner.adopt_session(session())
    async def run():
        async with AsyncDriver(owner) as driver:
            with pytest.raises(Exception, match="runtime" if cli_compat else "Executable") :
                await driver.perform(Send("GET", "/test", None, 1, True, None))
    asyncio.run(run())
    owner.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_async_jupyter_terminal_cleanup_and_application_seam(monkeypatch, cancel):
    from inspire.platform.web.transport import Transport
    from inspire.platform.web.transport_core import ApplicationRequest
    owner = Transport(None, "https://example.test", username="")
    owner.adopt_session(session())
    monkeypatch.setattr(terminal, "get_transport", lambda session: owner)
    events = []

    async def request(method, path, **kwargs):
        events.append((method, path))
        if "GetNotebookAccessUrl" in path:
            return {"Result": {"jupyter_url": "https://example.test/jupyter/lab"}}
        assert isinstance(kwargs["body"], ApplicationRequest)
        if method == "POST":
            return SimpleNamespace(status_code=201, json=lambda: {"name": "term"})
        return SimpleNamespace(status_code=200)

    async def capture(**kwargs):
        if cancel:
            raise asyncio.CancelledError
        return terminal.JupyterCommandResult(0, "done", True, "DONE")

    monkeypatch.setattr(owner, "request_async", request)
    monkeypatch.setattr(terminal, "_capture_terminal_output_async", capture)
    async def run():
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await terminal.run_command_capture_in_notebook_async(
                    notebook_id="fake", command="echo done", session=session(), marker="DONE")
        else:
            result = await terminal.run_command_capture_in_notebook_async(
                notebook_id="fake", command="echo done", session=session(), marker="DONE")
            assert result.output == "done"
    asyncio.run(run())
    assert [method for method, _ in events] == ["POST", "GET", "POST", "DELETE"]
    assert events[-1][1].endswith("/api/terminals/term")
    owner.close()
