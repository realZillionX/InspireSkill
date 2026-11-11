"""Offline CAS contracts: persistence, approval boundaries, SMS and terminal QR."""

from __future__ import annotations

import importlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from click.testing import CliRunner
from PIL import Image, ImageDraw

from inspire import accounts
from inspire.accounts.state_inventory import find_orphan_state as inspect_account_state
from inspire.cli.main import main
from inspire.platform.web.session import auth, cas
from inspire.platform.web.session.cas_qr import render_qr
from inspire.platform.web.session.models import WebSession

commands = importlib.import_module("inspire.cli.commands.login")
PHONE = "13800000000"
CODE = "123456"
COOKIE = dict(
    name="CASTGC",
    value="private-cookie",
    domain=cas.CAS_HOST,
    path="/cas",
    expires=None,
    secure=True,
)
FORM = '<form id="fm2"><input name="execution" value="e1s1"><input name="username" value="ignored"><input name="smscode" value="ignored"><input name="submit" value="Login"></form>'


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        requests.sessions.Session, "send", Mock(side_effect=AssertionError("Network forbidden"))
    )
    monkeypatch.setattr(cas, "resolve_requests_proxy_config", lambda **kw: ({}, "none"))
    monkeypatch.setattr(commands, "stdin_is_interactive", lambda: False)
    accounts.create_account("tester", '[api]\nbase_url="https://qz.sii.edu.cn"\n')
    return tmp_path


def response(status=200, text="", payload=None, location=""):
    return SimpleNamespace(
        status_code=status, text=text, headers={"Location": location}, json=lambda: payload
    )


def test_session_roundtrip_permissions_and_redaction():
    stored = cas.CasSession([COOKIE], time.time(), "sms")
    stored.save("tester")
    assert cas.CasSession.load("tester") == stored
    assert "private-cookie" not in repr(stored)
    path = cas.cache_path("tester", "cas_session.json")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    with stored.http("tester") as http:
        assert http.cookies.get("CASTGC") == "private-cookie"
        assert http.headers["User-Agent"] == cas.USER_AGENT
    path.write_text('{"cookies":null}')
    assert cas.CasSession.load("tester") is None
    path.unlink()
    assert cas.CasSession.load("tester") is None


@pytest.mark.parametrize(
    "bad", [[], None, {}, [dict(COOKIE, domain="sii.edu.cn")], [dict(COOKIE, expires="tomorrow")]]
)
def test_malformed_cache(bad):
    path = cas.cache_path("tester", "cas_session.json")
    path.write_text(json.dumps(dict(cookies=bad, created_at=time.time(), method="sms")))
    assert cas.CasSession.load("tester") is None


def test_sms_send_complete_and_pending(monkeypatch):
    with cas.build_cas_http([], "tester") as http:
        http.cookies.set("SESSION", "state", domain=cas.CAS_HOST, path="/cas")
        monkeypatch.setattr(http, "get", Mock(return_value=response(text=FORM)))
        post = Mock(return_value=response(payload={"success": True}))
        monkeypatch.setattr(http, "post", post)
        pending = cas.sms_send_code("+86" + PHONE, http)
        assert post.call_args.kwargs["data"] == {"request_username": PHONE, "isEn": "false"}
        pending.save("tester")
        path = cas.cache_path("tester", "cas_pending.json")
        assert PHONE not in path.read_text() and CODE not in path.read_text()
        assert "username" not in path.read_text() and "smscode" not in path.read_text()
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
        pending = cas.SmsPending.load("tester")
        http.cookies.set_cookie(requests.cookies.create_cookie(**COOKIE))
        post.return_value = response(302, location=cas.SERVICE + "?ticket=ST-test")
        stored = cas.sms_complete(PHONE, CODE, pending, http)
        assert stored.method == "sms"
        assert "submit" not in post.call_args.kwargs["data"]
        assert post.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "status,with_cookie,exception", [(200, False, cas.SmsCodeRejected), (302, False, cas.CasError)]
)
def test_sms_rejection_is_sanitized(monkeypatch, status, with_cookie, exception):
    with cas.build_cas_http([], "tester") as http:
        monkeypatch.setattr(
            http,
            "post",
            Mock(
                return_value=response(
                    status, text=PHONE + CODE, location=cas.SERVICE + "?ticket=ST-test"
                )
            ),
        )
        with pytest.raises(exception) as error:
            cas.sms_complete(PHONE, CODE, cas.SmsPending([], {"execution": "e"}, time.time()), http)
        assert PHONE not in str(error.value) and CODE not in str(error.value)


def test_pending_expiry_and_missing():
    with pytest.raises(cas.CasError, match="missing or expired"):
        cas.SmsPending.load("tester")
    cas.SmsPending([], {"execution": "e"}, time.time() - 601).save("tester")
    with pytest.raises(cas.CasError, match="missing or expired"):
        cas.SmsPending.load("tester")


@pytest.mark.parametrize("phone", ["student-id", "123", "23800000000", "１３８００００００００"])
def test_phone_validation(phone):
    with pytest.raises(cas.CasError):
        cas.validate_phone(phone)


@pytest.mark.parametrize("form", [False, True])
def test_device_approval(form):
    broker = (
        '<script>{"kcContext":{"url":"/realms/inf-internal/broker/cas/login?a=1&amp;b=2"}}</script>'
    )
    consent = (
        '<form action="/grant"><input name="code" value="opaque"><button name="accept" value="Yes">Accept</button><input name="cancel"></form>'
        if form
        else '<script>{"pageId":"login-oauth-grant","url":{"oauthAction":"\\/grant"},"oauth":{"code":"opaque"}}</script>'
    )
    http = Mock()
    http.request.side_effect = [
        response(text=broker),
        response(303, location="https://cas.sii.edu.cn/cas/login"),
        response(302, location="https://keycloak.test/consent"),
        response(text=consent),
        response(302, location="/device/status"),
        response(),
    ]
    assert cas.approve_device(
        http, "https://keycloak.test/device?user_code=test", "https://qz.sii.edu.cn"
    )
    calls = http.request.call_args_list
    assert all(c.kwargs["allow_redirects"] is False for c in calls)
    assert calls[4].args[:2] == ("POST", "https://keycloak.test/grant")
    assert calls[4].kwargs["data"] == {"code": "opaque", "accept": "Yes"}


@pytest.mark.parametrize(
    "page",
    [
        response(302, location="https://foreign.test/steal"),
        response(text='<form action="https://foreign.test/steal"><input name="accept"></form>'),
        response(text="unexpected page"),
        response(302, location="http://cas.sii.edu.cn/insecure"),
    ],
)
def test_approval_refuses_foreign_hosts_and_surprises(page):
    http = Mock()
    http.request.return_value = page
    assert not cas.approve_device(http, "https://keycloak.test/device", "https://qz.sii.edu.cn")
    assert http.request.call_count == 1


def test_qr_grid_and_fallback(tmp_path):
    image = Image.new("L", (29 * 5, 29 * 5), 255)
    draw = ImageDraw.Draw(image)
    for ox, oy in ((4, 4), (18, 4), (4, 18)):
        for y in range(7):
            for x in range(7):
                if x in (0, 6) or y in (0, 6) or (2 <= x <= 4 and 2 <= y <= 4):
                    draw.rectangle(
                        ((ox + x) * 5, (oy + y) * 5, (ox + x + 1) * 5 - 1, (oy + y + 1) * 5 - 1),
                        fill=0,
                    )
    path = tmp_path / "qr.png"
    image.save(path)
    result = render_qr(path)
    assert result and len(result.splitlines()) == 15
    assert "▀" in result and "█" in result and "\x1b[30;47m" in result
    Image.new("L", (50, 50), 255).save(path)
    assert render_qr(path) is None


def test_qr_grid_with_fractional_module_size(tmp_path):
    """Browser captures scale a module to a fractional pixel width (234 px / 33)."""
    size = 33
    grid = Image.new("L", (size, size), 255)
    for ox, oy in ((0, 0), (size - 7, 0), (0, size - 7)):
        for y in range(7):
            for x in range(7):
                if x in (0, 6) or y in (0, 6) or (2 <= x <= 4 and 2 <= y <= 4):
                    grid.putpixel((ox + x, oy + y), 0)
    grid.putpixel((size - 1, size - 1), 0)
    path = tmp_path / "qr.png"
    grid.resize((234, 234), Image.Resampling.NEAREST).save(path)
    result = render_qr(path)
    assert result is not None
    assert len(result.splitlines()) == (size + 8 + 1) // 2


def test_bare_cas_json_errors_before_login():
    result = CliRunner().invoke(main, ["login", "--cas", "--json"])
    assert result.exit_code != 0 and "Specify --cas sms or --cas feishu" in result.output


def test_cli_two_step(monkeypatch):
    monkeypatch.setattr(
        cas, "sms_send_code", Mock(return_value=cas.SmsPending([], {"execution": "e"}, time.time()))
    )
    monkeypatch.setattr(
        cas, "sms_complete", Mock(return_value=cas.CasSession([COOKIE], time.time(), "sms"))
    )
    monkeypatch.setattr(
        commands,
        "run_login",
        Mock(
            return_value=SimpleNamespace(
                login_username="tester", access_expires_at=1, refresh_expires_at=2
            )
        ),
    )
    args = ["login", "--account", "tester", "--cas", "sms", "--phone", PHONE, "--json"]
    first = CliRunner().invoke(main, args)
    assert first.exit_code == 0, first.output
    assert json.loads(first.stdout)["data"]["pending"]
    commands.run_login.assert_not_called()
    second = CliRunner().invoke(main, args + ["--sms-code", CODE])
    assert second.exit_code == 0, second.output
    assert cas.CasSession.load("tester") is not None
    assert not cas.cache_path("tester", "cas_pending.json").exists()
    assert PHONE not in first.output + second.output and CODE not in first.output + second.output


@pytest.mark.parametrize("approved", [True, False])
def test_login_auto_approval_or_manual_fallback(monkeypatch, capsys, approved):
    cas.CasSession([COOKIE], time.time(), "sms").save("tester")
    monkeypatch.setattr(cas, "cas_ticket_valid", lambda *a: True)
    monkeypatch.setattr(cas, "approve_device", lambda *a: approved)

    def device(account, notify):
        notify("https://keycloak.test/device", "user-code", 300)
        return Mock()

    monkeypatch.setattr(commands, "device_login", device)
    metadata = {}
    commands.run_login("tester", no_browser=True, cas_metadata=metadata)
    output = capsys.readouterr()
    assert ("https://keycloak.test/device" in output.err) is not approved
    assert metadata == {"cas": True, "cas_method": "sms"}


@pytest.mark.parametrize("expired", [True, False])
def test_silent_renewal_without_relocking(monkeypatch, expired):
    cached = WebSession(
        login_username="tester",
        access_token="old",
        refresh_token="old-refresh",
        access_expires_at=0,
        refresh_expires_at=0 if expired else time.time() + 1000,
        created_at=1,
        account="tester",
        base_url="https://qz.sii.edu.cn",
    )
    monkeypatch.setattr(WebSession, "load", lambda **kw: cached)
    cas.CasSession([COOKIE], time.time(), "sms").save("tester")
    monkeypatch.setattr(auth, "cas_ticket_valid", lambda *a: True)
    monkeypatch.setattr(auth, "approve_device", lambda *a: True)
    tokens = SimpleNamespace(
        access_token="new",
        refresh_token="new-refresh",
        access_expires_at=100,
        refresh_expires_at=200,
    )

    class Device:
        def __init__(self, *args):
            pass

        def refresh(self, token):
            raise auth.AuthenticationError("refused")

        def login(self, notify, *, deadline=None):
            notify("https://keycloak.test/device", "code", 300)
            return tokens

    monkeypatch.setattr(auth, "DeviceAuthorization", Device)
    monkeypatch.setattr(auth, "token_login_username", lambda token: "tester")
    monkeypatch.setattr(
        auth, "exclusive_session_refresh", Mock(side_effect=AssertionError("must not relock"))
    )
    saved = Mock()
    monkeypatch.setattr(auth, "_save_existing_account", saved)
    assert auth.refresh_cached("tester").access_token == "new"
    saved.assert_called_once()
    monkeypatch.setattr(auth, "cas_ticket_valid", lambda *a: False)
    with pytest.raises(auth.AuthenticationError, match="inspire login"):
        auth.refresh_cached("tester")


@pytest.mark.parametrize("outcome", ["success", "timeout", "missing-cookie", "render-error"])
def test_feishu_capture_private_file_and_cleanup(monkeypatch, outcome):
    from inspire.platform.web.session import cas_qr

    clock = [100.0]
    monkeypatch.setattr(cas_qr.time, "monotonic", lambda: clock[0])
    browser = Mock()
    context = browser.new_context.return_value
    page = context.new_page.return_value
    element = page.wait_for_selector.return_value
    frame = element.content_frame.return_value
    frame.query_selector.return_value = None
    element.screenshot.return_value = b"fake-png"
    context.cookies.return_value = (
        [dict(COOKIE, expires=-1), dict(COOKIE, domain="qz.sii.edu.cn")]
        if outcome != "missing-cookie"
        else []
    )
    shown = []

    def wait(milliseconds):
        clock[0] += milliseconds / 1000
        if shown and outcome in ("success", "missing-cookie"):
            predicate, intercept = context.route.call_args.args
            assert predicate("https://aip.sii.edu.cn/?ticket=ST-example")
            assert not predicate("https://aip.sii.edu.cn.foreign.test/?ticket=ST-example")
            intercept(Mock())

    page.wait_for_timeout.side_effect = wait
    playwright = Mock()
    playwright.__enter__ = Mock(return_value=playwright)
    playwright.__exit__ = Mock(return_value=False)
    playwright.chromium.launch.return_value = browser
    monkeypatch.setattr(cas_qr, "sync_playwright", lambda: playwright)
    monkeypatch.setattr(
        cas_qr, "get_playwright_proxy", lambda account: {"server": "http://proxy.test"}
    )

    def show(path):
        shown.append(path)
        assert path.read_bytes() == b"fake-png"
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
        if outcome == "render-error":
            raise ValueError("private diagnostics")

    if outcome == "success":
        stored = cas.feishu_qr_login(show, "tester", timeout=4)
        assert stored.method == "feishu"
        assert len(stored.cookies) == 1 and stored.cookies[0]["expires"] is None
    else:
        with pytest.raises(cas.CasError) as error:
            cas.feishu_qr_login(show, "tester", timeout=4)
        assert "private diagnostics" not in str(error.value)
    browser.close.assert_called_once()
    assert shown and not shown[0].exists()
    assert playwright.chromium.launch.call_args.kwargs["proxy"] == {"server": "http://proxy.test"}
    assert page.goto.call_args.kwargs["wait_until"] == "load"


def test_sms_interactive_retry_limit(monkeypatch):
    monkeypatch.setattr(commands, "stdin_is_interactive", lambda: True)
    send = Mock(return_value=cas.SmsPending([], {"execution": "e"}, time.time()))
    complete = Mock(side_effect=cas.SmsCodeRejected("Code rejected."))
    monkeypatch.setattr(cas, "sms_send_code", send)
    monkeypatch.setattr(cas, "sms_complete", complete)
    result = CliRunner().invoke(
        main,
        ["login", "--account", "tester", "--cas", "sms", "--phone", PHONE],
        input="111111\n222222\n333333\n",
    )
    assert result.exit_code != 0
    assert complete.call_count == 3 and send.call_count == 1
    assert cas.CasSession.load("tester") is None


@pytest.mark.parametrize("expired", [False, True])
def test_sms_cli_missing_expired_pending(monkeypatch, expired):
    if expired:
        cas.SmsPending([], {"execution": "e"}, time.time() - 601).save("tester")
    complete = Mock()
    monkeypatch.setattr(cas, "sms_complete", complete)
    result = CliRunner().invoke(
        main, ["login", "--account", "tester", "--cas", "sms", "--phone", PHONE, "--sms-code", CODE]
    )
    assert result.exit_code != 0 and "missing or expired" in result.output
    complete.assert_not_called()


def test_ticket_validation_and_foreign_redirect(monkeypatch):
    http = Mock()
    http.get.return_value = response(302, location="https://foreign.test/?ticket=private")
    with pytest.raises(cas.CasTicketExpired, match="CAS login expired"):
        cas.mint_ticket(http)
    http.get.return_value = response(302, location=cas.SERVICE + "?ticket=ST-example")
    assert cas.mint_ticket(http) == "ST-example"
    assert http.get.call_args.kwargs["allow_redirects"] is False


def test_silent_approval_failure_does_not_save_or_poll(monkeypatch):
    cached = WebSession(
        login_username="tester",
        access_token="old",
        refresh_token="old-refresh",
        access_expires_at=0,
        refresh_expires_at=0,
        created_at=1,
        account="tester",
        base_url="https://qz.sii.edu.cn",
    )
    monkeypatch.setattr(WebSession, "load", lambda **kw: cached)
    cas.CasSession([COOKIE], time.time(), "feishu").save("tester")
    monkeypatch.setattr(auth, "cas_ticket_valid", lambda *a: True)
    monkeypatch.setattr(auth, "approve_device", lambda *a: False)

    def login(notify, *, deadline=None):
        notify("https://keycloak.test/device", "code", 300)
        pytest.fail("Must not poll when approval failed")

    device = Mock()
    device.login.side_effect = login
    monkeypatch.setattr(auth, "DeviceAuthorization", Mock(return_value=device))
    saved = Mock()
    monkeypatch.setattr(auth, "_save_existing_account", saved)
    with pytest.raises(auth.AuthenticationError, match="inspire login"):
        auth.refresh_cached("tester")
    saved.assert_not_called()


def test_account_state_inventory_keeps_cas_files():
    for name in ("cas_session.json", "cas_pending.json", "cas_qr.png"):
        cas.cache_path("tester", name).write_text("test")
    assert not inspect_account_state()


@pytest.mark.parametrize("approved", [True, False])
def test_feishu_cli_autoapproval_and_json(monkeypatch, approved):
    monkeypatch.setattr(
        cas, "feishu_qr_login", lambda show, name: cas.CasSession([COOKIE], time.time(), "feishu")
    )
    monkeypatch.setattr(cas, "cas_ticket_valid", lambda *args: True)
    monkeypatch.setattr(cas, "approve_device", lambda *args: approved)
    browser = Mock()
    monkeypatch.setattr(commands.webbrowser, "open", browser)

    def device(name, notify):
        notify("https://keycloak.test/device", "device-user-code", 300)
        return SimpleNamespace(login_username="tester", access_expires_at=10, refresh_expires_at=20)

    monkeypatch.setattr(commands, "device_login", device)
    result = CliRunner().invoke(main, ["login", "--account", "tester", "--cas", "feishu", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["cas"] is True and data["cas_method"] == "feishu"
    assert ("https://keycloak.test/device" in result.stderr) is not approved
    browser.assert_not_called()


def test_requests_proxy_is_account_scoped(monkeypatch):
    resolve = Mock(return_value=({"https": "http://proxy.test:1234"}, "toml"))
    monkeypatch.setattr(cas, "resolve_requests_proxy_config", resolve)
    with cas.build_cas_http([COOKIE], "tester") as http:
        assert http.proxies["https"] == "http://proxy.test:1234"
        assert not http.trust_env
    resolve.assert_called_once_with(account="tester")


@pytest.mark.parametrize("inline", [False, True])
def test_cas_argv_secrets_absent_from_written_debug_log(monkeypatch, tmp_path, inline):
    from inspire.cli.logging_setup import configure_debug_logging, clear_debug_logging

    monkeypatch.setenv("INSPIRE_DEBUG_LOG_DIR", str(tmp_path / "logs"))
    args = [f"--phone={PHONE}", f"--sms-code={CODE}"] if inline else ["--phone", PHONE, "--sms-code", CODE]
    path = configure_debug_logging(argv=["inspire", "--debug", "login", "--cas", "sms", *args])
    clear_debug_logging()
    text = Path(path).read_text()
    assert "argv=" in text
    assert PHONE not in text and CODE not in text


@pytest.mark.parametrize("old,new,discovered", [
    ("A", "B", None), ("A", "A", None), (None, "A", None),
    ("A", None, None), ("A", None, "B"), ("A", None, "A"),
])
def test_silent_renewal_requires_same_known_identity(monkeypatch, old, new, discovered):
    cached = WebSession("old", "refresh", 0, 0, 1, account="tester", login_username=old,
                        base_url="https://platform.test", user_detail={"old": True})
    cas.CasSession([COOKIE], time.time(), "sms").save("tester")
    monkeypatch.setattr(auth, "cas_ticket_valid", lambda *a: True)
    monkeypatch.setattr(auth, "token_login_username", lambda token: new)
    tokens = SimpleNamespace(access_token="new", refresh_token="refresh", access_expires_at=10,
                             refresh_expires_at=20)
    device = Mock()
    device.login.return_value = tokens
    monkeypatch.setattr(auth, "DeviceAuthorization", Mock(return_value=device))

    def discover(session, base_url, **kwargs):
        assert session.user_detail is None
        assert session.login_username is None
        session.login_username = discovered

    monkeypatch.setattr(auth, "_discover_identity", discover)
    save = Mock()
    monkeypatch.setattr(auth, "_save_existing_account", save)
    if old and old == (new or discovered):
        assert auth._renew_with_cas(cached, "tester").login_username == old
        save.assert_called_once()
    else:
        with pytest.raises(auth.AuthenticationError, match="inspire login"):
            auth._renew_with_cas(cached, "tester")
        save.assert_not_called()
    assert cached.access_token == "old"


def test_explicit_cas_login_rebuilds_identity_even_if_discovery_fails(monkeypatch):
    old = WebSession("old", "refresh", 10, 20, 1, account="tester", login_username="A",
                     user_detail={"old": True}, all_workspace_ids=["old-workspace"])
    old.save(account="tester")
    cas.CasSession([COOKIE], time.time(), "sms").save("tester")
    monkeypatch.setattr(cas, "cas_ticket_valid", lambda *a: True)
    monkeypatch.setattr(cas, "approve_device", lambda *a: True)
    monkeypatch.setattr(auth, "token_login_username", lambda token: "B")
    device = Mock()
    device.login.return_value = SimpleNamespace(access_token="new", refresh_token="new-refresh",
                                               access_expires_at=time.time()+100, refresh_expires_at=time.time()+200)
    monkeypatch.setattr(auth, "DeviceAuthorization", Mock(return_value=device))
    monkeypatch.setattr(auth, "_discover_identity", lambda *a: None)
    result = commands.run_login("tester")
    assert result.login_username == "B"
    assert not result.user_detail and not result.all_workspace_ids
    stored = WebSession.load(account="tester")
    assert stored.login_username == "B"
    assert not stored.user_detail and not stored.all_workspace_ids


@pytest.mark.parametrize("kind", ["session", "pending", "qr"])
def test_logout_interleaved_before_cas_publication(monkeypatch, kind):
    from contextlib import contextmanager
    from inspire.platform.web.session import cas_qr
    from inspire.services.account.lifecycle import delete_local_account

    original = cas.exclusive_session_refresh

    @contextmanager
    def logout_first(account):
        delete_local_account(account)
        with original(account):
            yield

    monkeypatch.setattr(cas, "exclusive_session_refresh", logout_first)
    with pytest.raises(auth.AuthenticationError, match="Account tester was removed"):
        if kind == "session":
            cas.CasSession([COOKIE], time.time(), "sms").save("tester")
        elif kind == "pending":
            cas.SmsPending([], {"execution": "e"}, time.time()).save("tester")
        else:
            cas_qr._publish_qr("tester", None, b"qr")
    assert not accounts.account_dir("tester").exists()


def test_qr_logins_have_independent_atomic_private_files(monkeypatch):
    from inspire.platform.web.session import cas_qr

    first = cas_qr._publish_qr("tester", None, b"first")
    second = cas_qr._publish_qr("tester", None, b"second")
    assert first != second
    replace = os.replace

    def checked_replace(source, target):
        assert target.read_bytes() == b"first"
        assert source.read_bytes() == b"updated"
        replace(source, target)

    monkeypatch.setattr(cas_qr.os, "replace", checked_replace)
    assert cas_qr._publish_qr("tester", first, b"updated") == first
    first.unlink()
    assert second.read_bytes() == b"second"
    if os.name != "nt":
        assert second.stat().st_mode & 0o777 == 0o600
    for name in ("cas_session.json.lock", "cas_pending.json.lock"):
        cas.cache_path("tester", name).touch()
    assert not inspect_account_state()
    second.unlink()


@pytest.mark.parametrize("args,code", [
    (["--cas"], 12), (["--cas", "sms", "--phone", "invalid"], 12),
    (["--phone", PHONE], 12), (["--sms-code", CODE], 12),
    (["--cas", "feishu", "--phone", PHONE], 12), (["--cas", "sms"], 12),
    (["--cas", "sms", "--phone", PHONE, "--sms-code", CODE], 11),
    (["--cas", "feishu"], 11),
])
def test_cas_errors_are_json_on_stdout(monkeypatch, args, code):
    monkeypatch.setattr(cas, "feishu_qr_login", Mock(side_effect=cas.CasError("CAS failed")))
    result = CliRunner().invoke(main, ["login", "--account", "tester", "--json", *args])
    assert result.exit_code == code, result.output
    envelope = json.loads(result.stdout)
    assert envelope["success"] is False
    assert envelope["error"]["type"] == ("ValidationError" if code == 12 else "AuthenticationError")


def test_expired_pending_json():
    cas.SmsPending([], {"execution": "e"}, time.time()-601).save("tester")
    result = CliRunner().invoke(main, ["--json", "login", "--account", "tester", "--cas", "sms",
                                       "--phone", PHONE, "--sms-code", CODE])
    assert result.exit_code == 11
    assert "expired" in json.loads(result.stdout)["error"]["message"]


def test_overlapping_qr_logins_cancel_only_their_own_file(monkeypatch):
    from inspire.platform.web.session import cas_qr

    clock = [0.0]
    monkeypatch.setattr(cas_qr.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cas_qr, "get_playwright_proxy", lambda account: None)

    def playwright_factory():
        playwright = Mock()
        playwright.__enter__ = Mock(return_value=playwright)
        playwright.__exit__ = Mock(return_value=False)
        context = playwright.chromium.launch.return_value.new_context.return_value
        page = context.new_page.return_value
        element = page.wait_for_selector.return_value
        element.content_frame.return_value.query_selector.return_value = None
        element.screenshot.return_value = b"png"
        page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
        return playwright

    monkeypatch.setattr(cas_qr, "sync_playwright", playwright_factory)
    paths = []

    def cancel_second(path):
        paths.append(path)
        assert paths[0] != path and paths[0].read_bytes() == b"png"
        raise ValueError("cancel second")

    def show_first(path):
        paths.append(path)
        with pytest.raises(cas.CasError):
            cas_qr.feishu_qr_login(cancel_second, "tester", timeout=10)
        assert path.read_bytes() == b"png"
        assert not paths[1].exists()
        raise ValueError("cancel first")

    with pytest.raises(cas.CasError):
        cas_qr.feishu_qr_login(show_first, "tester", timeout=10)
    assert len(paths) == 2 and all(not path.exists() for path in paths)


def test_silent_renewal_caps_the_whole_flow_and_restores_budget(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    cached = WebSession("old", "refresh", 0, 0, 1, account="tester", login_username="A",
                        base_url="https://platform.test")
    cas.CasSession([COOKIE], time.time(), "sms").save("tester")

    def valid(*args):
        assert cas.request_timeout(300) == 60
        clock[0] += 20
        return True

    def login(notify, *, deadline):
        assert deadline == 160
        assert cas.request_timeout(300) == 40
        clock[0] = deadline
        return SimpleNamespace(access_token="new", refresh_token="refresh",
                               access_expires_at=100, refresh_expires_at=200)

    monkeypatch.setattr(auth, "cas_ticket_valid", valid)
    monkeypatch.setattr(auth, "token_login_username", lambda token: "A")
    device = Mock()
    device.login.side_effect = login
    monkeypatch.setattr(auth, "DeviceAuthorization", Mock(return_value=device))
    save = Mock()
    monkeypatch.setattr(auth, "_save_existing_account", save)
    with pytest.raises(auth.AuthenticationError, match="timed out"):
        auth._renew_with_cas(cached, "tester")
    save.assert_not_called()
    assert cas.renewal_deadline.get() is None


def test_cas_success_oauth_failure_cannot_later_switch_identity(monkeypatch):
    old = WebSession("old", "refresh", 0, 0, 1, account="tester", login_username="A",
                     base_url="https://platform.test", user_detail={"identity": "A"})
    old.save(account="tester")
    monkeypatch.setattr(cas, "feishu_qr_login", lambda *args: cas.CasSession([COOKIE], time.time(), "feishu"))
    monkeypatch.setattr(cas, "cas_ticket_valid", lambda *args: True)
    monkeypatch.setattr(auth, "cas_ticket_valid", lambda *args: True)
    device = Mock()
    device.login.side_effect = auth.AuthenticationError("OAuth failed")
    monkeypatch.setattr(auth, "DeviceAuthorization", Mock(return_value=device))
    result = CliRunner().invoke(main, ["login", "--account", "tester", "--cas", "feishu", "--json"])
    assert result.exit_code == 11
    assert cas.CasSession.load("tester") is not None
    cached = WebSession.load(account="tester", allow_expired=True)
    assert cached.login_username == "A" and cached.access_token == "old"
    device.login.side_effect = None
    device.login.return_value = SimpleNamespace(access_token="new", refresh_token="new-refresh",
                                               access_expires_at=10, refresh_expires_at=20)
    monkeypatch.setattr(auth, "token_login_username", lambda token: "B")
    with pytest.raises(auth.AuthenticationError, match="inspire login"):
        auth._renew_with_cas(cached, "tester")
    saved = WebSession.load(account="tester", allow_expired=True)
    assert saved.access_token == "old" and saved.user_detail == {"identity": "A"}


@pytest.mark.parametrize("kind", ["session", "pending"])
def test_cas_writer_never_recreates_directory_after_existence_check(monkeypatch, kind):
    original = cas.atomic_write_text

    def removed_before_write(path, content, **kwargs):
        accounts.remove_account("tester")
        return original(path, content, **kwargs)

    monkeypatch.setattr(cas, "atomic_write_text", removed_before_write)
    with pytest.raises(auth.AuthenticationError, match="Account tester was removed"):
        if kind == "session":
            cas.CasSession([COOKIE], time.time(), "sms").save("tester")
        else:
            cas.SmsPending([], {"execution": "e"}, time.time()).save("tester")
    assert not accounts.account_dir("tester").exists()
