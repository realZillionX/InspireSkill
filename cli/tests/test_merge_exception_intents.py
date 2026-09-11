"""Offline regressions for exception intent carried into the shared SDK layers."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
import requests

from inspire.platform.errors import SubmissionUncertainError
from inspire.platform.web import pty_socket, transport_core
from inspire.platform.web.session import browser_client, WebSession
from inspire.platform.web.session import requests as session_requests
from inspire.platform.web.transport import Transport
from inspire.services.execution import notebook_targets


@pytest.mark.parametrize("stage", ["config", "bridge", "probe"])
def test_moved_target_failures_are_logged(monkeypatch, caplog, stage):
    error = RuntimeError("unavailable")

    def fail(*args, **kwargs):
        raise error

    config = SimpleNamespace(get_bridge=fail)
    monkeypatch.setattr(
        notebook_targets.tunnel_module,
        "load_tunnel_config",
        fail if stage == "config" else lambda: config,
    )
    monkeypatch.setattr(notebook_targets.tunnel_module, "is_tunnel_available", fail)
    caplog.set_level(logging.DEBUG, logger=notebook_targets.__name__)
    if stage == "probe":
        candidate = SimpleNamespace(bridge=SimpleNamespace(name="demo"), config=config)
        assert notebook_targets.target_available(candidate) is False
    else:
        assert (
            notebook_targets.candidate_from_cache_entry(
                entry={"bridge_name": "demo"}, notebook="demo", workspace=None
            )
            is None
        )
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info[1] is error
    assert "trying next strategy" in caplog.records[0].message


@pytest.mark.parametrize("cli_compat", [False, True])
@pytest.mark.parametrize("write", [False, True])
def test_core_logs_read_fallback_but_classifies_dispatched_write(caplog, cli_compat, write):
    shared = transport_core.SharedState()
    if write:
        shared.write = {"used": False, "sent": True, "create": True, "operation_id": "op"}
    core = transport_core.RequestCore(
        shared, cli_compat=cli_compat, allow_browser=True, timeout=30, deadline=None
    )
    caplog.set_level(logging.DEBUG, logger=transport_core.__name__)
    program = core.run("POST", "/test", {}, 30, None)
    assert isinstance(next(program), transport_core.Observe)
    assert isinstance(program.send(transport_core.Observation(0, 1, 0)), transport_core.Send)
    error = requests.ConnectionError("lost response")
    outcome = program.throw(error)
    if write:
        assert isinstance(outcome, transport_core.Raise)
        assert isinstance(outcome.error, SubmissionUncertainError)
        assert outcome.error.__cause__ is error
        assert not caplog.records
    else:
        assert isinstance(outcome, transport_core.Observe)
        assert any("trying browser fallback" in r.message and r.exc_info for r in caplog.records)
    program.close()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("status", [401, 429, 500])
def test_browser_body_failure_keeps_status_classification(caplog, asynchronous, status):
    error = OSError("body read failed")

    def fail():
        raise error

    async def async_fail():
        fail()

    response = SimpleNamespace(status=status, headers={}, text=async_fail if asynchronous else fail)
    caplog.set_level(logging.DEBUG, logger=browser_client.__name__)
    expected = (
        browser_client.SessionExpiredError if status == 401 else browser_client.TransientAPIError
    )
    if asynchronous:

        async def run():
            async def get(*args, **kwargs):
                return response

            client = browser_client.AsyncBrowserRequestClient(
                WebSession(storage_state={}, created_at=1)
            )
            client._context = SimpleNamespace(request=SimpleNamespace(get=get))
            with pytest.raises(expected):
                await client.request_json("GET", "https://example.test")

        asyncio.run(run())
    else:
        client = object.__new__(browser_client._BrowserRequestClient)
        client._closed = False
        client._context = SimpleNamespace(request=SimpleNamespace(get=lambda *a, **kw: response))
        with pytest.raises(expected):
            client.request_json("GET", "https://example.test")
    assert any(
        "classifying HTTP status" in r.message and r.exc_info[1] is error for r in caplog.records
    )


@pytest.mark.parametrize("asynchronous", [False, True])
def test_browser_cleanup_attempts_every_resource(asynchronous):
    calls = []

    def fail(name):
        calls.append(name)
        raise OSError("already closed")

    async def async_fail(name):
        fail(name)

    if asynchronous:
        client = browser_client.AsyncBrowserRequestClient(
            WebSession(storage_state={}, created_at=1)
        )
        client._context = SimpleNamespace(close=lambda: async_fail("context"))
        client._browser = SimpleNamespace(close=lambda: async_fail("browser"))
        client._playwright = SimpleNamespace(stop=lambda: async_fail("runtime"))
        asyncio.run(client.close())
        asyncio.run(client.close())
    else:
        client = object.__new__(browser_client._BrowserRequestClient)
        client._closed = False
        client._context = SimpleNamespace(close=lambda: fail("context"))
        client._browser = SimpleNamespace(close=lambda: fail("browser"))
        client._playwright = SimpleNamespace(stop=lambda: fail("runtime"))
        client.close()
        client.close()
    assert calls == ["context", "browser", "runtime"]


def test_pooled_cleanup_continues_after_one_failure(monkeypatch):
    calls = []

    def close(name):
        calls.append(name)
        raise OSError("already closed")

    pool = {i: SimpleNamespace(close=lambda i=i: close(i)) for i in range(2)}
    monkeypatch.setattr(session_requests, "_pooled_by_thread", pool)
    session_requests.close_pooled_requests_session()
    assert calls == [0, 1]
    assert pool == {}


def test_owned_transport_cleanup_continues_after_failures(monkeypatch):
    calls = []

    def close(name):
        calls.append(name)
        raise OSError("already closed")

    transport = Transport(None, "https://example.test", username="")
    monkeypatch.setattr(transport, "reset_plaza_client", lambda: close("plaza"))
    transport._browser = SimpleNamespace(close=lambda: close("browser"))
    transport._http = SimpleNamespace(close=lambda: close("http"))
    transport.close()
    transport.close()
    assert calls == ["plaza", "browser", "http"]
    assert transport._closed
    assert transport._http is transport._browser is transport._session is None


@pytest.mark.parametrize("cancel", [False, True])
def test_async_pty_releases_writer_after_close_frame_failure(cancel):
    calls = []

    async def run():
        client = pty_socket.AsyncWebSocketClient("ws://example.test", {})

        async def send(*args):
            if cancel:
                raise asyncio.CancelledError
            raise OSError("peer gone")

        def close():
            calls.append("close")
            raise OSError("already closed")

        async def wait_closed():
            calls.append("wait")
            raise OSError("already closed")

        client._send_frame = send
        client.writer = SimpleNamespace(close=close, wait_closed=wait_closed)
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await client.close()
        else:
            await client.close()
        assert client.writer is None

    asyncio.run(run())
    assert calls == ["close", "wait"]


@pytest.mark.parametrize("failed_request", [False, True])
def test_async_http_cleanup_preserves_request_outcome(failed_request):
    from inspire.platform.web.transport_async import AsyncDriver

    calls = []
    request_error = RuntimeError("request failed")

    async def close(name):
        calls.append(name)
        raise OSError("already closed")

    async def run():
        transport = Transport(None, "https://example.test", username="")
        try:
            async with AsyncDriver(transport) as driver:
                driver.stack.push_async_callback(close, "first")
                driver.stack.push_async_callback(close, "second")
                if failed_request:
                    raise request_error
                return "accepted"
        finally:
            transport.close()

    if failed_request:
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(run())
        assert caught.value is request_error
    else:
        assert asyncio.run(run()) == "accepted"
    assert calls == ["second", "first"]
