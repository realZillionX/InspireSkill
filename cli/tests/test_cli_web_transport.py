"""Offline CLI/transport compatibility contracts, including the old output oracle."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from click.testing import CliRunner

from inspire.cli.main import main
from inspire.cli.utils import web_transport
from inspire.platform.web import session as ws
from inspire.platform.web.browser_api import core
from inspire.platform.web.runtime import active_transport, session_transport
from inspire.platform.web.transport import Transport


@pytest.fixture
def cli_session(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("INSPIRE_BASE_URL", "https://example.test")
    core.clear_browser_api_runtime_cache()
    session = ws.WebSession(
        storage_state={"cookies": [{"name": "session", "value": "old"}]},
        created_at=1,
        base_url="https://example.test",
        account="chosen",
    )
    monkeypatch.setattr(ws, "_get_web_session", lambda **kwargs: session)
    monkeypatch.setattr(ws.WebSession, "load", lambda **kwargs: None)
    monkeypatch.setattr(ws, "_renew_web_session_without_credentials", lambda _: None)
    commands = importlib.import_module("inspire.cli.commands.notebook.notebook_commands")
    monkeypatch.setattr(commands, "resolve_workspace_operation_scope", lambda **kwargs: "ws-test")
    monkeypatch.setattr(commands, "_resolve_notebook_id", lambda *args, **kwargs: ("nb-test", None))
    monkeypatch.setattr(
        requests.Session, "send", lambda *a, **k: pytest.fail("real HTTP forbidden")
    )
    yield session
    core.clear_browser_api_runtime_cache()


def response(status=200, payload=None):
    def json():
        if isinstance(payload, Exception):
            raise payload
        return {"Result": {}} if payload is None else payload

    return SimpleNamespace(status_code=status, text="platform refused", headers={}, json=json)


def invoke(json_output=False):
    args = ["--json"] if json_output else []
    return CliRunner().invoke(main, [*args, "notebook", "stop", "demo", "--workspace", "space"])


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("status", [403, 404, 408, 425, 429, 500, 501, 503])
def test_cli_transport_preserves_legacy_error_output(monkeypatch, cli_session, status, json_output):
    seen = []

    def post(*args, **kwargs):
        seen.append(active_transport.get())
        return response(status)

    monkeypatch.setattr(ws, "pooled_requests_session", lambda *args: SimpleNamespace(post=post))
    monkeypatch.setattr("time.sleep", lambda _: None)
    # Captured against the legacy path before L2 removed its dispatcher.
    message = f"Failed to stop notebook: API returned {status}: platform refused"
    expected = (
        '{"success":false,"error":{"type":"APIError","code":13,"message":"' + message + '"}}\n'
        if json_output
        else f"Error: {message}\n"
    )
    after = invoke(json_output)
    assert (after.exit_code, after.output) == (13, expected)
    assert seen and all(isinstance(t, Transport) for t in seen)
    assert all(t._closed for t in seen)
    assert active_transport.get() is None
    assert session_transport.get() is None


@pytest.mark.parametrize("failure", [ValueError("not JSON"), requests.ConnectionError("offline")])
def test_cli_fallback_adopts_session_without_deadline(monkeypatch, cli_session, failure):
    transports = []
    now = [0.0]
    monkeypatch.setattr("inspire.platform.web.transport.time.monotonic", lambda: now[0])

    def post(*args, **kwargs):
        transport = active_transport.get()
        transports.append(transport)
        assert transport.session is cli_session
        assert transport.account == "chosen"
        assert transport.allow_browser is True
        assert transport.deadline is None
        assert transport._write is None
        now[0] += 86400
        assert transport.remaining() > 0
        if isinstance(failure, requests.ConnectionError):
            raise failure
        return response(payload=failure)

    def browser_request(*args, **kwargs):
        assert active_transport.get() is transports[0]
        assert kwargs["headers"]["Referer"].startswith("https://example.test")
        return {"Result": {}}

    monkeypatch.setattr(ws, "pooled_requests_session", lambda *args: SimpleNamespace(post=post))
    monkeypatch.setattr(
        ws, "_get_browser_client", lambda _: SimpleNamespace(request_json=browser_request)
    )
    result = invoke()
    assert result.exit_code == 0, result.output
    assert len(transports) == 1
    assert transports[0]._force_browser is True
    assert transports[0]._closed


def test_cli_failure_from_transport_keeps_command_contract(monkeypatch, cli_session):
    def fail(*args, **kwargs):
        raise RuntimeError("fake transport failure")

    monkeypatch.setattr(Transport, "request", fail)
    after = invoke(True)
    assert after.exit_code == 13
    assert after.output == (
        '{"success":false,"error":{"type":"APIError","code":13,"message":'
        '"Failed to stop notebook: fake transport failure"}}\n'
    )


def test_no_deadline_scope_preserves_sdk_default_and_nested_deadline(monkeypatch):
    monkeypatch.setattr("inspire.platform.web.transport.time.monotonic", lambda: 10)
    transport = Transport("chosen", "https://example.test", username="test")
    with transport.scope(timeout=None):
        assert transport.deadline is None
        with transport.scope():
            assert transport.deadline == 130
            with transport.scope(timeout=None):
                assert transport.deadline == 130
        assert transport.deadline is None
    transport.close()


def test_cli_base_override_account_scope_and_cleanup(monkeypatch, cli_session):
    import click
    from inspire.accounts import account_scope, current_account

    @click.command()
    def command():
        web_transport.install_web_transport()
        with account_scope("chosen"):
            session = ws.get_web_session()
            transport = active_transport.get()
            assert transport.session is session
            assert current_account() == "chosen"
            core._set_base_url("https://override.test/")
            assert core._get_base_url() == "https://override.test"
            with account_scope("another"):
                assert current_account() == "another"
            assert current_account() == "chosen"
        assert current_account() is None

    with account_scope(None):
        result = CliRunner().invoke(command)
    assert result.exit_code == 0, result.exception
    assert active_transport.get() is None
    assert not (Path.home() / ".inspire" / "current").exists()


def test_shared_transport_refresh_is_in_place_and_budgeted(monkeypatch, cli_session):
    transport = Transport(
        "chosen", "https://example.test", username="", cli_compat=True, allow_browser=True
    )
    transport.adopt_session(cli_session)
    logins = []

    def login(**kwargs):
        logins.append(kwargs)
        return ws.WebSession(
            storage_state={"cookies": [{"name": "session", "value": "new"}]},
            created_at=2,
            account="chosen",
            cookies={"session": "new"},
        )

    monkeypatch.setattr(ws, "_get_web_session", login)
    monkeypatch.setattr(
        ws,
        "pooled_requests_session",
        lambda *args: SimpleNamespace(get=lambda *a, **k: response(401)),
    )
    for _ in range(5):
        with pytest.raises(ws.SessionExpiredError, match="refused as well"):
            transport.request("GET", "/test")
    assert len(logins) == 1
    assert transport.session is cli_session
    assert cli_session.cookies == {"session": "new"}
    assert cli_session.created_at == 2
    transport.close()


def test_unscoped_calls_share_default_transport_and_clamp_paging(monkeypatch, cli_session):
    from inspire.platform.web.runtime import get_transport, close_default_transports

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return response()

    monkeypatch.setattr(ws, "pooled_requests_session", lambda *args: SimpleNamespace(post=post))
    monkeypatch.setattr(ws.WebSession, "load", lambda **kwargs: pytest.fail("must adopt, not load"))
    first = get_transport(cli_session)
    assert first is get_transport(cli_session)
    assert first.deadline is None and first.allow_browser
    body = {"page_size": 10000}
    for _ in range(2):
        core._request_json(
            cli_session,
            "POST",
            "/test",
            referer="https://example.test/page",
            body=body,
            timeout=600,
        )
    assert first.session is cli_session
    assert len(calls) == 2
    assert calls[0][1]["json"] == {"page_size": 5000}
    assert calls[0][1]["timeout"] == 600
    assert body == {"page_size": 10000}
    close_default_transports()
    assert first._closed


def test_default_fallback_and_login_guards_are_account_local(monkeypatch, cli_session):
    from inspire.platform.web.runtime import get_transport

    other = ws.WebSession(
        storage_state=cli_session.storage_state,
        created_at=1,
        base_url=cli_session.base_url,
        account="another",
    )
    first, second = get_transport(cli_session), get_transport(other)
    first._force_browser = True
    first._unproven_rebuild = 2
    assert second is not first
    assert second._force_browser is False
    assert second._unproven_rebuild is None
    assert get_transport(cli_session) is first


def test_default_base_override_does_not_log_in(monkeypatch, cli_session):
    from inspire.platform.web.runtime import get_transport

    monkeypatch.setattr(ws, "get_web_session", lambda **k: pytest.fail("URL lookup must be lazy"))
    assert core._get_base_url() == "https://example.test"
    core._set_base_url("https://override.test/")
    assert core._get_base_url() == "https://override.test"
    assert get_transport(cli_session).base_url == "https://override.test"


def test_sdk_scope_wins_over_cli_session_factory(monkeypatch, cli_session):
    from inspire.platform.web.runtime import get_transport

    transport = Transport("chosen", "https://example.test", username="test")
    token = session_transport.set(lambda _: pytest.fail("SDK must keep its own transport"))
    try:
        with transport.scope():
            assert get_transport(cli_session) is transport
    finally:
        session_transport.reset(token)
        transport.close()


def test_threads_share_one_refresh_and_the_original_session(monkeypatch, cli_session):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from inspire.platform.web.runtime import get_transport

    barrier = Barrier(8)
    logins = []

    def get(url, **kwargs):
        if cli_session.created_at == 1:
            barrier.wait(timeout=5)
            return response(401)
        return response()

    def login(**kwargs):
        logins.append(kwargs)
        return ws.WebSession(
            storage_state={"cookies": [{"name": "session", "value": "new"}]},
            cookies={"session": "new"},
            created_at=2,
            account="chosen",
        )

    monkeypatch.setattr(ws, "pooled_requests_session", lambda *args: SimpleNamespace(get=get))
    monkeypatch.setattr(ws, "_get_web_session", login)
    transport = get_transport(cli_session)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: transport.request("GET", "/test"), range(8)))
    assert results == [{"Result": {}}] * 8
    assert len(logins) == 1
    assert transport.session is cli_session
    assert cli_session.cookies == {"session": "new"}


def test_cli_worker_first_use_closes_in_click_context(monkeypatch, cli_session):
    import click
    from contextvars import copy_context
    from concurrent.futures import ThreadPoolExecutor
    from inspire.platform.web.runtime import get_transport

    seen = []

    @click.command()
    def command():
        web_transport.install_web_transport()
        with ThreadPoolExecutor(max_workers=1) as pool:
            seen.append(pool.submit(copy_context().run, get_transport, cli_session).result())

    result = CliRunner().invoke(command)
    assert result.exit_code == 0, result.exception
    assert len(seen) == 1 and seen[0]._closed
    assert active_transport.get() is None


def test_missing_storage_is_not_misclassified_as_non_json(monkeypatch, cli_session):
    from inspire.platform.web.runtime import get_transport

    cli_session.storage_state = {}
    monkeypatch.setattr(ws, "_get_browser_client", lambda _: pytest.fail("not a body failure"))
    with pytest.raises(ValueError, match="missing storage state"):
        get_transport(cli_session).request("GET", "/test")


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("status", [300, 301, 302, 307, 308, 399, 400, 401, 403, 404,
                                   408, 425, 429, 499, 500, 501, 502, 503, 504, 599])
def test_shared_dispatch_cli_policy_preserves_error_bytes(
    monkeypatch, cli_session, method, status
):
    from inspire.platform.web.transport import _CLI_POLICY

    text = "平台拒绝\n" + "x" * 600
    reply = response(status)
    reply.text = text
    reply.headers = {"Retry-After": "7"}
    calls = []

    def send(url, **kwargs):
        calls.append((url, kwargs))
        return reply

    monkeypatch.setattr(
        ws, "pooled_requests_session",
        lambda *args: SimpleNamespace(**{method.lower(): send}),
    )
    transport = Transport("chosen", cli_session.base_url, username="", cli_compat=True)
    transport.adopt_session(cli_session)
    # Literal legacy status set: do not let the oracle follow an implementation change.
    expired = status == 401 or 300 <= status < 400
    transient = status in {408, 425, 429, 500, 502, 503, 504}
    expected_type = ws.SessionExpiredError if expired else (
        ws.TransientAPIError if transient else ValueError
    )
    expected_message = "Session expired or invalid" if expired else f"API returned {status}: {text}"
    try:
        with pytest.raises(expected_type) as caught:
            transport._once(method, "/test", None, 17, policy=_CLI_POLICY)
        assert type(caught.value) is expected_type
        assert str(caught.value).encode("utf-8") == expected_message.encode("utf-8")
        if transient:
            assert caught.value.status == status
            assert caught.value.retry_after == 7
        kwargs = {"headers": {}, "timeout": 17, "allow_redirects": False}
        if method == "POST":
            kwargs.update(headers={"Content-Type": "application/json"}, json={})
        assert calls == [("https://example.test/test", kwargs)]
    finally:
        transport.close()


def test_shared_dispatch_cli_policy_preserves_non_json_error(monkeypatch, cli_session):
    from inspire.platform.web.transport import _CLI_POLICY, _NonJSONResponse

    failure = ValueError("Invalid JSON: 平台\n" + "x" * 600)
    monkeypatch.setattr(
        ws, "pooled_requests_session",
        lambda *args: SimpleNamespace(get=lambda *a, **k: response(payload=failure)),
    )
    transport = Transport("chosen", cli_session.base_url, username="", cli_compat=True)
    transport.adopt_session(cli_session)
    try:
        with pytest.raises(_NonJSONResponse) as caught:
            transport._once("GET", "/test", None, 17, policy=_CLI_POLICY)
        assert str(caught.value).encode("utf-8") == str(failure).encode("utf-8")
        assert caught.value.__cause__ is failure
    finally:
        transport.close()


def test_explicit_adoption_replaces_stale_session_identity(cli_session):
    import click
    from dataclasses import replace

    acquired = replace(cli_session, created_at=2)
    with click.Context(click.Command("test")):
        web_transport.install_web_transport()
        adopt = session_transport.get()
        transport = adopt(cli_session)
        # Seed stale state explicitly: lazy loading must not rescue missing adoption.
        transport._session = cli_session
        assert adopt(acquired) is transport
        assert transport._session is acquired
        assert transport.session is acquired
