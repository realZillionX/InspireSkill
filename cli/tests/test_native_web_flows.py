"""Full renewal/plaza programs, scripted at the two HTTP backend boundaries."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
from urllib.parse import urlsplit

import httpx
import pytest
import requests

from inspire.platform.web import session as ws
from inspire.platform.web.session import auth, proxy, refresh_lock
from inspire.platform.web.transport import Transport
from inspire.platform.web.transport_async import AsyncDriver
from inspire.platform.web.flow import call
from inspire.platform.web.transport_core import remaining

BASE = "https://console.test"
FORM = '<script src="/rsa.js"></script><form id="fm1" action="/cas/submit"><input name="username"><input name="password"></form>'
CAPTCHA = '<input name="captcha"><img src="/cas/captcha.jpg">'
DETAIL = {"Result": {"id": "same-user"}}


def session(generation=1):
    return ws.WebSession(
        storage_state={
            "cookies": [
                {"name": "inspire-session", "value": "old", "domain": "console.test", "path": "/"},
                {"name": "CASTGC", "value": "old-sso", "domain": "cas.sii.edu.cn", "path": "/"},
            ]
        },
        user_detail={"id": "same-user"},
        login_username="test",
        base_url=BASE,
        created_at=generation,
    )


def scripted_run(monkeypatch, asynchronous, scenario, cli=False):
    owner = Transport(None, BASE, username="test", cli_compat=cli, allow_browser=False)
    owner.adopt_session(session())
    trace = []
    clock = [0.0]
    counters = {"login": 0, "ticket": 0, "api": 0}
    plaza = scenario.startswith("plaza-")

    def cached(**kwargs):
        trace.append(("cache",))
        return session(3) if scenario == "fresh-cache" else None

    def persist(value, **kwargs):
        trace.append(("persist", bool(value.cookies.get("CASTGC"))))

    def credentials(*args):
        trace.append(("credentials",))
        return "test", "unused"

    def guard(*args, **kwargs):
        trace.append(("guard",))
        return nullcontext()

    def response(method, url, headers, body, follow, jar):
        parts = urlsplit(url)
        trace.append(("http", method, url, headers, body, follow))
        status, data, extra = 200, {}, {}
        if parts.netloc == "cas.sii.edu.cn":
            counters["ticket"] += 1
            refused = scenario in {"plaza-cas", "plaza-cas-still-dead"} and (
                counters["ticket"] <= 2 or scenario == "plaza-cas-still-dead"
            )
            status = 200 if refused else 302
            data = FORM if refused else ""
            if not refused:
                extra["Location"] = "https://aip.sii.edu.cn/?ticket=ST-test"
        elif parts.netloc == "aip.sii.edu.cn":
            if parts.path.endswith("/login"):
                data = {"code": 0, "data": {"userInfo": {"ID": "42"}}}
                jar.set("datasets-session", "plaza", domain=parts.netloc, path="/")
            else:
                counters["api"] += 1
                failures = {"plaza-cookie": 1, "plaza-second": 2}.get(scenario, 0)
                status = 401 if counters["api"] <= failures else 200
                data = {"code": 7} if status == 401 else {"code": 0, "data": {"ok": True}}
        elif parts.path == "/login":
            counters["login"] += 1
            if scenario == "sso-error":
                raise requests.ConnectionError("SSO unavailable")
            if scenario == "deadline":
                clock[0] = 2.0
            gone = scenario in {"credentials", "verification", "plaza-cas", "plaza-cas-still-dead"}
            data = FORM if gone else "<html>signed in</html>"
            if scenario == "verification" and counters["login"] == 2:
                data = FORM.replace("</form>", CAPTCHA + "</form>")
            if not gone:
                jar.set("inspire-session", "renewed", domain="console.test", path="/")
        elif parts.path == "/rsa.js":
            data = 'RSAUtils.getKeyPair("10001", "", "' + "ff" * 64 + '")'
        elif parts.path == "/cas/submit":
            jar.set("inspire-session", "new", domain="console.test", path="/")
            jar.set("CASTGC", "fresh", domain="cas.sii.edu.cn", path="/")
        elif "GetUserDetail" in parts.query:
            data = DETAIL
        else:
            data = {"Result": {}}
        content = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        return status, content, extra

    def sync_send(http, request, **kwargs):
        status, content, extra = response(
            request.method,
            request.url,
            dict(request.headers),
            request.body.encode() if isinstance(request.body, str) else request.body,
            kwargs["allow_redirects"],
            http.cookies,
        )
        result = requests.Response()
        result.status_code, result._content = status, content
        result.url, result.request = request.url, request
        result.headers.update(extra)
        return result

    async def async_send(http, request, **kwargs):
        await asyncio.sleep(0)
        headers = {k.decode(): v.decode() for k, v in request.headers.raw if k.lower() != b"host"}
        try:
            status, content, extra = response(
                request.method,
                str(request.url),
                headers,
                request.content or None,
                kwargs["follow_redirects"],
                http.cookies,
            )
        except requests.ConnectionError as error:
            raise httpx.ConnectError(str(error)) from error
        return httpx.Response(status, content=content, headers=extra, request=request)

    async def run():
        async with AsyncDriver(owner) as driver:
            if plaza:
                return await driver.execute(call(owner.plaza_request, "GET", "/api/read"))
            action = call(owner._refresh_cli, 1, can_refresh=True) if cli else call(owner._refresh)
            await driver.execute(action)
            return owner.session.user_detail

    with monkeypatch.context() as patch:
        patch.setattr(ws.WebSession, "load", cached)
        patch.setattr(auth, "_persist", persist)
        patch.setattr(auth, "get_credentials", credentials)
        patch.setattr(auth, "guarded_credential_submission", guard)
        patch.setattr(refresh_lock, "exclusive_session_refresh", lambda *a, **kw: nullcontext())
        patch.setattr(auth, "_cas_page_encrypt_password", lambda *a: "encrypted")
        patch.setattr(proxy, "resolve_requests_proxy_config", lambda **kw: ({}, "none"))
        patch.setattr(requests.Session, "send", sync_send)
        patch.setattr(httpx.AsyncClient, "send", async_send)
        original_to_thread = asyncio.to_thread

        async def local_io_only(function, *args, **kwargs):
            from functools import partial

            leaf = function.func if isinstance(function, partial) else function
            assert leaf.__name__ in {
                "_configure", "_load_runtime_config", "_load_proxy_toml_values", "_persist", "_close_browser_client",
                "_prepare", "prepare", "build", "load", "save", "get_session_cache_file", "is_dir",
            }, f"unexpected offload: {leaf.__name__}"
            return await original_to_thread(function, *args, **kwargs)

        patch.setattr(asyncio, "to_thread", local_io_only)
        if scenario == "deadline":
            owner.deadline = 1
            patch.setattr(owner, "remaining", lambda: remaining(30, owner.deadline, clock[0]))
        try:
            if asynchronous:
                result = asyncio.run(run())
            elif plaza:
                result = owner.plaza_request("GET", "/api/read")
            else:
                if cli:
                    owner._refresh_cli(1, can_refresh=True)
                else:
                    owner._refresh()
                result = owner.session.user_detail
        except Exception as error:
            result = (type(error).__name__, str(error))
        finally:
            owner.close()
    return trace, result


@pytest.mark.parametrize(
    "scenario",
    [
        "fresh-cache",
        "sso",
        "credentials",
        "sso-error",
        "verification",
        "deadline",
        "plaza-cookie",
        "plaza-cas",
        "plaza-second",
        "plaza-cas-still-dead",
    ],
)
@pytest.mark.parametrize("cli", [False, True], ids=["sdk", "cli"])
def test_native_renewal_and_plaza_parity(monkeypatch, scenario, cli):
    synchronous = scripted_run(monkeypatch, False, scenario, cli)
    asynchronous = scripted_run(monkeypatch, True, scenario, cli)
    assert asynchronous == synchronous
    trace, result = synchronous
    credentials = [event for event in trace if event[0] == "credentials"]
    http = [event for event in trace if event[0] == "http"]
    if scenario == "fresh-cache":
        assert not http and not credentials
    elif scenario in {"sso", "plaza-cookie", "plaza-second"}:
        assert not credentials
        assert result == ({"ok": True} if scenario.startswith("plaza") else {"id": "same-user"})
    elif scenario in {"credentials", "plaza-cas"}:
        assert len(credentials) == 1
        assert result == ({"ok": True} if scenario.startswith("plaza") else {"id": "same-user"})
    elif scenario == "sso-error":
        assert not credentials and result == (
            "ConnectionError" if cli else "AuthenticationError",
            "SSO unavailable",
        )
    elif scenario == "verification":
        assert "verification code" in result[1]
        assert not any(event[1] == "POST" for event in http)
    elif scenario == "deadline":
        assert not credentials and result[0] == "WaitTimeoutError"
    elif scenario == "plaza-cas-still-dead":
        assert len(credentials) == 1 and "single-sign-on ticket" in result[1]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_cooldown_stops_a_second_native_credential_submission(monkeypatch, tmp_path, asynchronous):
    from inspire.platform.errors import AuthenticationCooldownError
    from inspire.platform.web.session import login_guard

    owner = Transport(None, BASE, username="test")
    owner.adopt_session(session())
    sent = []
    monkeypatch.setattr(ws.WebSession, "load", lambda **kw: None)
    monkeypatch.setattr(auth, "renew_web_session_without_credentials", lambda _: None)
    monkeypatch.setattr(auth, "get_credentials", lambda _: ("test", "dummy-password"))
    monkeypatch.setattr(login_guard, "block_file", lambda _: tmp_path / "login-block.json")
    monkeypatch.setattr(login_guard, "credential_fingerprint", lambda *a: "dummy-fingerprint")
    monkeypatch.setattr(refresh_lock, "exclusive_session_refresh", lambda *a, **kw: nullcontext())

    def rejected(*args, **kwargs):
        sent.append(True)
        raise auth.AuthenticationError("temporary CAS rejection")

    monkeypatch.setattr(auth, "_login_with_cas_requests", rejected)

    async def run():
        async with AsyncDriver(owner) as driver:
            await driver.execute(call(owner._refresh))

    try:
        for _ in range(2):
            with pytest.raises(AuthenticationCooldownError) as raised:
                asyncio.run(run()) if asynchronous else owner._refresh()
            assert raised.value.retry_at > 0
        assert sent == [True]
    finally:
        owner.close()


def test_native_lock_wait_is_cancellable_and_releases_descriptor(tmp_path):
    from inspire.accounts.cache_lock import exclusive_cache_lock
    from inspire.platform.web.async_context import cache_lock_async

    owner = Transport(None, BASE, username="test")
    path = tmp_path / "session"
    entered = []

    async def borrower():
        async with cache_lock_async(path, owner):
            entered.append(True)

    async def run():
        with exclusive_cache_lock(path):
            task = asyncio.create_task(borrower())
            await asyncio.sleep(0.02)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.wait_for(borrower(), 1)
        assert entered == [True]

    asyncio.run(run())
    owner.close()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_rebuild_guard_protects_cas_ticket_escalation(monkeypatch, asynchronous):
    owner = Transport(None, BASE, username="test")
    owner.adopt_session(session())
    owner._decisions.rebuilt(1)
    monkeypatch.setattr(auth, "get_credentials", lambda *a: pytest.fail("unused login replaced"))

    async def run():
        async with AsyncDriver(owner) as driver:
            await driver.execute(call(owner._refresh, require_cas_ticket=True))

    try:
        with pytest.raises(ws.SessionExpiredError, match="nothing has been able to use"):
            asyncio.run(run()) if asynchronous else owner._refresh(require_cas_ticket=True)
        owner._decisions.success(1, 1, False)
        assert owner._unproven_rebuild is None
    finally:
        owner.close()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_external_application_dispatch_renews_and_keeps_xsrf(monkeypatch, asynchronous):
    from inspire.platform.web.session import requests as preparation
    from inspire.platform.web.application import ApplicationConnection
    from inspire.platform.web.transport_core import ApplicationRequest

    owner = Transport(None, BASE, username="test")
    owner.adopt_session(session())
    target = "https://notebook.test/api/terminals"
    http = requests.Session()
    http.cookies.set("inspire-session", "old", domain="notebook.test", path="/")
    connection = ApplicationConnection(owner, http, 1)
    http.cookies.set("_xsrf", "application", domain="notebook.test", path="/")
    seen = []
    monkeypatch.setattr(preparation, "resolve_requests_proxy_config", lambda: ({}, "none"))

    def refresh():
        new = session(2)
        new.storage_state["cookies"] = [
            {
                "name": "inspire-session",
                "value": "fresh",
                "domain": "notebook.test",
                "path": "/",
            }
        ]
        owner.adopt_session(new)

    def reply(url, cookie, timeout):
        assert url == target
        assert timeout <= 3
        seen.append(cookie)
        return 401 if len(seen) == 1 else 201

    def sync_send(http, request, **kwargs):
        result = requests.Response()
        result.status_code = reply(request.url, request.headers["Cookie"], kwargs["timeout"][1])
        result._content = b'{"name": "one"}'
        return result

    async def async_send(http, request, **kwargs):
        status = reply(
            str(request.url), request.headers["Cookie"], request.extensions["timeout"]["read"]
        )
        return httpx.Response(status, json={"name": "one"}, request=request)

    monkeypatch.setattr(owner, "_refresh", refresh)
    monkeypatch.setattr(requests.Session, "send", sync_send)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
    body = ApplicationRequest(connection, {"headers": {"X-XSRFToken": "application"}})
    try:
        with owner.scope(timeout=3):
            response = (
                asyncio.run(owner.request_async("POST", target, body=body))
                if asynchronous
                else owner.request("POST", target, body=body)
            )
        assert response.json() == {"name": "one"}
        assert "inspire-session=old" in seen[0]
        assert "inspire-session=fresh" in seen[1] and "_xsrf=application" in seen[1]
    finally:
        http.close()
        owner.close()
