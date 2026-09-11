"""Run the real interpreters with scripted HTTP, browser, refresh and clock I/O."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, is_dataclass
from typing import Any

import httpx
import pytest
import requests

from inspire.platform.web import session as ws
from inspire.platform.web.transport import Transport
from inspire.platform.web.transport_core import Observation, RequestCore, Raise, Return

PATH = "/api/v2/test?Action=Test"
OK = {"Result": {"ok": True}}


def describe(value: Any) -> Any:
    if isinstance(value, Exception):
        return (type(value).__name__, str(value), describe(value.__cause__))
    if is_dataclass(value):
        return (type(value).__name__, describe(asdict(value)))
    if isinstance(value, dict):
        return {key: describe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [describe(item) for item in value]
    return value


SCENARIOS = {
    "success": ([200], None, None),
    "429-storm": ([429, 429, 429], None, None),
    "refresh": ([401, 200], None, None),
    "redirect": ([302, 200], None, None),
    "connection-error": (["network", 200], None, None),
    "write-401": ([401], "create", None),
    "write-500": ([500], "mutation", None),
    "write-429": ([429], "create", None),
    "non-json": (["html", 200], None, None),
    "deadline": ([429, 200], None, 0.05),
    "single-send": ([], "used", None),
    "refresh-refused": ([401, 401], None, None),
    "fallback-refresh": (["html", 401, 200], None, None),
    "envelope-storm": (["throttle"] * 3, None, None),
}


@pytest.mark.parametrize("cli_compat", [False, True], ids=["sdk", "cli"])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_drivers_have_identical_actions_and_outcome(monkeypatch, cli_compat, scenario):
    statuses, write, deadline = SCENARIOS[scenario]

    def run(asynchronous):
        script = list(statuses)
        actions, effects = [], []
        now = [0.0]
        transport = Transport(
            None, "https://example.test", username="", cli_compat=cli_compat, allow_browser=True
        )
        session = ws.WebSession(
            storage_state={"cookies": [{"name": "session", "value": "old"}]},
            created_at=1,
            base_url="https://example.test",
        )
        transport.adopt_session(session)
        transport.deadline = deadline
        if write:
            transport._write = {
                "used": write == "used",
                "sent": False,
                "create": write == "create",
                "operation_id": "operation",
            }

        def observe():
            return Observation(now[0], transport.session.created_at, 0.5)

        def wait(delay):
            effects.append(("sleep", delay))
            now[0] += delay

        async def async_wait(delay):
            wait(delay)

        def refresh():
            effects.append(("refresh", session.created_at))
            return ws.WebSession(
                storage_state={"cookies": [{"name": "session", "value": "new"}]},
                created_at=2,
                base_url="https://example.test",
            )

        def reply(browser=False):
            status = script.pop(0)
            if status == "html":
                return 200, b"<html>login</html>"
            payload = (
                {"ResponseMetadata": {"Error": {"Code": "Throttling"}}}
                if (status == "throttle")
                else OK
            )
            return 200 if status == "throttle" else status, json.dumps(payload).encode()

        def sync_send(http, request, **kwargs):
            effects.append(
                (
                    "send",
                    request.method,
                    request.url,
                    request.body,
                    dict(request.headers),
                    kwargs["allow_redirects"],
                )
            )
            if script[0] == "network":
                script.pop(0)
                raise requests.ConnectionError("offline")
            status, content = reply()
            response = requests.Response()
            response.status_code, response._content = status, content
            response.headers["Retry-After"] = "0.1"
            return response

        async def async_send(http, request, **kwargs):
            # HTTPX lowercases header lookup, but preserves the raw wire casing.
            headers = {
                key.decode(): value.decode()
                for key, value in request.headers.raw
                if key.lower() != b"host"
            }
            effects.append(
                (
                    "send",
                    request.method,
                    str(request.url),
                    request.content or None,
                    headers,
                    kwargs["follow_redirects"],
                )
            )
            if script[0] == "network":
                script.pop(0)
                raise httpx.ConnectError("offline")
            status, content = reply()
            return httpx.Response(status, content=content, headers={"Retry-After": "0.1"})

        class Browser:
            def request_json(self, method, url, **kwargs):
                effects.append(("browser", method, url, kwargs))
                status, content = reply(True)
                # Same browser error mapping is exercised by both real adapters.
                if status == 401:
                    raise ws.SessionExpiredError("browser expired")
                return json.loads(content)

            def close(self):
                pass

        class AsyncBrowser(Browser):
            def __init__(self, session):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                self.close()

            async def request_json(self, *args, **kwargs):
                return super().request_json(*args, **kwargs)

        original_run = RequestCore.run

        def traced(core, *args):
            program = original_run(core, *args)
            try:
                action = next(program)
                while True:
                    actions.append(describe(action))
                    if isinstance(action, (Return, Raise)):
                        yield action
                        return
                    try:
                        result = yield action
                    except Exception as error:
                        action = program.throw(error)
                    else:
                        action = program.send(result)
            finally:
                program.close()

        with monkeypatch.context() as patch:
            patch.setattr(RequestCore, "run", traced)
            patch.setattr(transport, "_observe", observe)
            patch.setattr("time.sleep", wait)
            patch.setattr(asyncio, "sleep", async_wait)
            patch.setattr(requests.Session, "send", sync_send)
            patch.setattr(httpx.AsyncClient, "send", async_send)
            patch.setattr(ws, "get_browser_client", lambda _: Browser())
            patch.setattr(ws, "create_browser_client", lambda _: Browser())
            patch.setattr("inspire.platform.web.session.browser_client.AsyncBrowserRequestClient", AsyncBrowser)
            patch.setattr(Transport, "_refresh", lambda self: self._adopt_session(refresh()))
            patch.setattr(Transport, "_refresh_expired_session", lambda self, _: refresh())
            try:
                if asynchronous:
                    outcome = asyncio.run(transport.request_async("POST", PATH, body={"a": 1}))
                else:
                    outcome = transport.request("POST", PATH, body={"a": 1})
            except Exception as error:
                outcome = describe(error)
            finally:
                final_state = (
                    transport._force_browser,
                    transport._unproven_rebuild,
                    describe(transport._write),
                )
                transport.close()
        return actions, effects, outcome, final_state

    sync = run(False)
    asynchronous = run(True)
    assert asynchronous == sync
    # Literal expectations stop a shared regression from making both drivers agree wrongly.
    actions, effects, outcome, _ = sync
    sends = [event for event in effects if event[0] in {"send", "browser"}]
    refreshes = [event for event in effects if event[0] == "refresh"]
    if scenario == "success":
        assert outcome == OK and len(sends) == 1
    elif scenario == "429-storm":
        assert len(sends) == 3 and not refreshes
    elif scenario in {"refresh", "redirect"}:
        assert len(refreshes) == 1 and len(sends) == 2 and outcome == OK
    elif scenario.startswith("write-"):
        assert len(sends) == 1 and not refreshes
        expected = {
            "write-401": "SubmissionUncertainError",
            "write-500": "MutationUncertainError",
            "write-429": "TransportError",
        }
        assert outcome[0] == expected[scenario]
    elif scenario in {"non-json", "connection-error"}:
        assert [item[0] for item in sends] == ["send", "browser"] and outcome == OK
    elif scenario == "deadline":
        assert len(sends) == 1 and outcome[0] == "WaitTimeoutError"
    elif scenario == "single-send":
        assert not effects and outcome[0] == "_SingleSendViolation"
    assert actions[-1][0] in {"Return", "Raise"}


def test_core_can_be_driven_without_http_or_clock_patching():
    from inspire.platform.web.transport_core import SharedState, Observe, Send, Sleep
    from inspire.platform.web.session.models import TransientAPIError

    program = RequestCore(
        SharedState(), cli_compat=False, allow_browser=False, timeout=30, deadline=5
    ).run("POST", PATH, {}, 30, None)
    assert isinstance(next(program), Observe)
    send = program.send(Observation(4, 1, 0.5))
    assert isinstance(send, Send) and send.timeout == 1
    assert isinstance(program.throw(TransientAPIError("busy", status=429)), Observe)
    assert program.send(Observation(4.95, 1, 0.5)) == Sleep(5 - 4.95)
    assert isinstance(program.send(None), Observe)
    action = program.send(Observation(5, 1, 0.5))
    assert isinstance(action, Raise) and type(action.error).__name__ == "WaitTimeoutError"
    program.close()


@pytest.mark.parametrize("cli_compat", [False, True], ids=["sdk", "cli"])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("proxy_mode", ["none", "system", "bypass", "explicit"])
def test_httpx_preserves_prepared_request_and_proxy_settings(
    monkeypatch,
    tmp_path,
    cli_compat,
    method,
    proxy_mode,
):
    from inspire.platform.web.session import requests as preparation
    from inspire.platform.web.transport_async import AsyncDriver
    from inspire.platform.web.transport_core import Send

    for name in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NETRC", str(tmp_path / "absent-netrc"))
    proxy = "http://proxy.test:8080"
    if proxy_mode in {"system", "bypass", "explicit"}:
        monkeypatch.setenv("HTTPS_PROXY", proxy)
    if proxy_mode in {"bypass", "explicit"}:
        monkeypatch.setenv("NO_PROXY", "example.test")
    monkeypatch.setattr(
        preparation,
        "resolve_requests_proxy_config",
        lambda: ({"https": proxy}, "toml") if proxy_mode == "explicit" else ({}, "system_env"),
    )
    session = ws.WebSession(
        storage_state={
            "cookies": [
                {"name": "same", "value": "root", "domain": "example.test", "path": "/"},
                {"name": "same", "value": "api", "domain": "example.test", "path": "/api"},
                {"name": "foreign", "value": "omit", "domain": "elsewhere.test", "path": "/"},
                {"name": "wrong_path", "value": "omit", "domain": "example.test", "path": "/other"},
            ]
        },
        created_at=1,
        base_url="https://example.test",
    )
    transport = Transport(None, session.base_url, username="", cli_compat=cli_compat)
    transport.adopt_session(session)
    captured, clients = [], []
    original_client = httpx.AsyncClient

    def factory(**kwargs):
        clients.append(dict(kwargs))
        kwargs.pop("proxy")
        return original_client(**kwargs)

    def sync_send(http, request, **kwargs):
        captured.append(
            (
                request.method,
                request.url,
                dict(request.headers),
                request.body,
                kwargs["allow_redirects"],
                kwargs["timeout"],
            )
        )
        response = requests.Response()
        response.status_code, response._content = 200, b"{}"
        return response

    async def async_send(http, request, **kwargs):
        captured.append(
            (
                request.method,
                str(request.url),
                {k.decode(): v.decode() for k, v in request.headers.raw if k != b"Host"},
                request.content or None,
                kwargs["follow_redirects"],
                request.extensions["timeout"],
            )
        )
        # Prove no sync send or worker is used for ordinary JSON traffic.
        await asyncio.sleep(0)
        http.cookies.set("drive-by", "must-not-leak", domain="example.test", path="/")
        return httpx.Response(200, json={})

    monkeypatch.setattr(requests.Session, "send", sync_send)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    action = Send(method, PATH, {"text": "中文", "n": 1}, 17, False, "https://example.test/ui")
    try:
        transport._once(method, PATH, action.body, 17, referer=action.referer)

        async def run():
            async with AsyncDriver(transport) as driver:
                await driver.perform(action)
                await driver.perform(action)
                pool = list(driver.clients.values())
            assert all(client.is_closed for client in pool)

        asyncio.run(run())
        assert captured[0][:5] == captured[1][:5] == captured[2][:5]
        assert "same=api; same=root" == captured[0][2]["Cookie"]
        assert captured[0][5] == (17 if cli_compat else (10, 17))
        assert captured[1][5] == {
            "connect": 17 if cli_compat else 10,
            "read": 17,
            "write": 17,
            "pool": 17,
        }
        assert len(clients) == 1
        assert clients[0]["proxy"] == (proxy if proxy_mode in {"system", "explicit"} else None)
        assert clients[0]["trust_env"] is False and clients[0]["follow_redirects"] is False
    finally:
        transport.close()


@pytest.mark.parametrize("cli_compat", [False, True])
def test_async_native_refresh_preserves_owner_and_browser_affinity(monkeypatch, cli_compat):
    import threading
    from inspire.platform.web.transport_async import AsyncDriver
    from inspire.platform.web.transport_core import Observe, Refresh, Send
    from inspire.platform.errors import ClientThreadError

    transport = Transport(None, "https://example.test", username="", cli_compat=cli_compat)
    thread = threading.get_ident()
    events = []
    session = ws.WebSession(
        storage_state={"cookies": [{"name": "session", "value": "old"}]},
        created_at=1,
        base_url="https://example.test",
    )
    monkeypatch.setattr(ws.WebSession, "load", lambda **kwargs: session)
    monkeypatch.setattr(ws, "get_web_session", lambda **kwargs: session)

    def refresh(worker):
        assert threading.get_ident() == thread
        worker.check()
        refreshed = ws.WebSession(
            storage_state={"cookies": [{"name": "session", "value": "new"}]},
            created_at=2,
            base_url="https://example.test",
        )
        worker._adopt_session(refreshed)

    monkeypatch.setattr(Transport, "_refresh", refresh)
    monkeypatch.setattr(Transport, "_refresh_cli", lambda self, *a, **kw: refresh(self))

    class Browser:
        def __init__(self, current):
            self.thread = threading.get_ident()
            assert self.thread == thread and current.created_at == 2

        async def request_json(self, *args, **kwargs):
            assert threading.get_ident() == self.thread
            events.append("send")
            return OK

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            assert threading.get_ident() == self.thread
            events.append("close")

    monkeypatch.setattr("inspire.platform.web.session.browser_client.AsyncBrowserRequestClient", Browser)

    async def run():
        async with AsyncDriver(transport) as driver:
            await driver.perform(Observe())
            await driver.perform(Refresh(1, True))
            assert transport.session.created_at == 2
            assert transport._thread == thread
            for _ in range(2):
                assert await driver.perform(Send("POST", PATH, {}, 30, True, None)) == OK
            if not cli_compat:
                with pytest.raises(ClientThreadError):
                    await asyncio.to_thread(transport.check)
        transport.check()

    try:
        asyncio.run(run())
        assert events == ["send", "close", "send", "close"]
        assert transport._browser is None
    finally:
        transport.close()
