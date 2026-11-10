"""Token cache and explicit authorization contracts; all network calls are mocked."""
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
import json
import time

import pytest
from click.testing import CliRunner

from inspire import accounts
from inspire.platform.web.session import auth
from inspire.platform.web.session.models import WebSession, AuthenticationError
from inspire.platform.web.session.oauth import TokenResponse


@pytest.fixture
def account(tmp_path, monkeypatch, active_account_session_storage):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    accounts.create_account("alice", '[api]\nbase_url="https://platform.test"\n')
    accounts.set_current_account("alice")
    return "alice"


def session(account, *, expired=False):
    return WebSession(access_token="old-access", refresh_token="old-refresh",
                      access_expires_at=time.time() + (-10 if expired else 3600),
                      refresh_expires_at=time.time() + 6000, created_at=time.time(),
                      base_url="https://platform.test", account=account)


def test_old_cookie_cache_is_logged_out(account):
    path = accounts.account_dir(account) / "web_session.json"
    path.write_text(json.dumps({"storage_state": {"cookies": []}, "created_at": time.time()}))
    assert WebSession.load(account=account) is None
    with pytest.raises(AuthenticationError, match="inspire login"):
        auth.get_web_session(account=account)


def test_cached_session_never_starts_device_flow(account, monkeypatch):
    cached = session(account)
    cached.save()
    monkeypatch.setattr(auth, "device_login", Mock(side_effect=AssertionError("implicit login")))
    assert auth.get_web_session().access_token == "old-access"


def test_expiry_refresh_persists_rotation(account, monkeypatch):
    cached = session(account, expired=True)
    cached.save()
    refresh = Mock(return_value=TokenResponse("new-access", "new-refresh", time.time()+3600, time.time()+6000))
    monkeypatch.setattr(auth.DeviceAuthorization, "refresh", refresh)
    result = auth.get_web_session()
    assert result.access_token == "new-access"
    assert WebSession.load().refresh_token == "new-refresh"
    refresh.assert_called_once_with("old-refresh")
    assert "new-refresh" not in repr(result)


def test_refresh_rereads_after_lock(account, monkeypatch):
    from contextlib import contextmanager
    stale = session(account, expired=True)
    stale.save()
    newer = replace(stale, access_token="other-access", refresh_token="other-refresh",
                    access_expires_at=time.time()+3600, created_at=stale.created_at+1)

    @contextmanager
    def lock(*args, **kwargs):
        newer.save()
        yield

    monkeypatch.setattr(auth, "exclusive_session_refresh", lock)
    monkeypatch.setattr(auth.DeviceAuthorization, "refresh", Mock(side_effect=AssertionError("duplicate refresh")))
    assert auth.get_web_session().access_token == "other-access"


def test_refresh_rejection_requires_explicit_login(account, monkeypatch):
    session(account, expired=True).save()
    monkeypatch.setattr(auth.DeviceAuthorization, "refresh", Mock(side_effect=AuthenticationError("invalid_grant")))
    with pytest.raises(AuthenticationError, match="inspire login"):
        auth.get_web_session()


def test_cli_login_logout_metadata_only(account, monkeypatch):
    import importlib
    commands = importlib.import_module("inspire.cli.commands.login")
    from inspire.cli.main import main
    cached = session(account)

    def login(alias, notify):
        notify("https://identity.test/device?user_code=ABCD", "ABCD", 600)
        cached.save()
        return cached

    monkeypatch.setattr(commands, "device_login", login)
    runner = CliRunner()
    result = runner.invoke(main, ["login", "--no-browser", "--json"])
    assert result.exit_code == 0, result.output
    assert "ABCD" in result.stderr
    assert "old-access" not in result.output
    assert "old-refresh" not in result.output
    assert "alice" in result.stdout
    result = runner.invoke(main, ["logout", "--yes"])
    assert result.exit_code == 0, result.output
    assert WebSession.load() is None


def test_requests_and_websocket_headers_are_platform_only(account):
    import requests
    from inspire.platform.web.session.requests import build_requests_session
    from inspire.platform.web.pty_socket import build_remote_cmd_headers
    cached = session(account)
    with build_requests_session(cached, "https://notebook.test/lab") as http:
        for url, expected in [("https://platform.test/api", True), ("https://notebook.test/lab", False)]:
            prepared = http.prepare_request(requests.Request("GET", url))
            assert ("Authorization" in prepared.headers) is expected
            assert ("X-Inspire-Client-Source" in prepared.headers) is expected
    headers = build_remote_cmd_headers(cached, base_url="https://platform.test")
    assert headers["Authorization"] == "Bearer old-access"
    assert headers["X-Inspire-Client-Source"].startswith("inspire-cli/")
    assert "Cookie" not in headers


def test_cli_login_mock_http_discovers_and_persists(account, monkeypatch):
    import requests
    from inspire.cli.main import main
    from inspire.platform.web.session import oauth
    original_init = oauth.DeviceAuthorization.__init__
    monkeypatch.setattr(oauth.DeviceAuthorization, "__init__",
                        lambda self, base_url, http: original_init(self, base_url, http, sleep=lambda _: None))
    seen = []

    def send(http, prepared, **kwargs):
        seen.append(prepared)
        if prepared.url.endswith("device-code"):
            payload = {"code": 0, "data": {"device_code": "device-secret", "user_code": "ABCD",
                       "verification_uri_complete": "https://identity.test/device?user_code=ABCD",
                       "expires_in": 600, "interval": 5}}
        elif prepared.url.endswith("/token"):
            payload = {"code": 0, "data": {"access_token": "access-secret", "refresh_token": "refresh-secret",
                       "expires_in": 3600, "refresh_expires_in": 6000}}
        elif "GetUserDetail" in prepared.url:
            payload = {"Result": {"extra_info": {"login_name": "login-user"}}}
        else:
            payload = {"Result": {"items": []}}
        if "/api/v2/" in prepared.url:
            assert prepared.headers["Authorization"] == "Bearer access-secret"
            assert prepared.headers["X-Inspire-Client-Source"].startswith("inspire-cli/")
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    result = CliRunner().invoke(main, ["login", "--no-browser", "--json"])
    assert result.exit_code == 0, result.output
    assert len(seen) == 4
    assert "secret" not in result.output
    assert "login-user" in result.stdout
    cached = WebSession.load(account=account)
    assert cached.refresh_token == "refresh-secret"
    assert cached.user_detail == {"extra_info": {"login_name": "login-user"}}


def test_account_add_authorizes_without_password(account, monkeypatch):
    from inspire.cli.main import main
    import importlib
    commands = importlib.import_module("inspire.cli.commands.login")
    calls = []
    monkeypatch.setattr(commands, "run_login", lambda alias, **kwargs: calls.append(alias))
    result = CliRunner().invoke(main, ["account", "add", "second", "--non-interactive",
                                     "--username", "ignored", "--password", "secret-password"])
    assert result.exit_code == 0, result.output
    assert calls == ["second"]
    assert "Deprecated" in result.stderr
    assert "secret-password" not in result.output
    config = accounts.account_config_path("second").read_text()
    assert "secret-password" not in config and "password" not in config


def test_login_does_not_open_browser_for_redirected_stdout(account, monkeypatch):
    from inspire.cli.main import main
    import importlib
    commands = importlib.import_module("inspire.cli.commands.login")
    opened = Mock()
    monkeypatch.setattr(commands.webbrowser, "open", opened)
    def authorize(alias, notify):
        notify("https://identity.test/device", "ABCD", 600)
        return session(alias)
    monkeypatch.setattr(commands, "device_login", authorize)
    result = CliRunner().invoke(main, ["login"])
    assert result.exit_code == 0, result.output
    opened.assert_not_called()


def test_browser_request_headers_only_for_platform(account):
    from types import SimpleNamespace
    from inspire.platform.web.session.browser_client import _BrowserRequestClient
    client = object.__new__(_BrowserRequestClient)
    client._closed = False
    client.session = session(account)
    captured = []
    def get(url, **kwargs):
        captured.append(kwargs["headers"])
        return SimpleNamespace(status=200, json=lambda: {})
    client._context = SimpleNamespace(request=SimpleNamespace(get=get))
    client.request_json("GET", "https://platform.test/api")
    client.request_json("GET", "https://notebook.test/api")
    assert captured[0]["Authorization"] == "Bearer old-access"
    assert "Authorization" not in captured[1]
    assert "X-Inspire-Client-Source" not in captured[1]


def jwt_for(username):
    import base64

    payload = base64.urlsafe_b64encode(
        json.dumps({"preferred_username": username}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


@pytest.mark.parametrize("failed", ["detail", "workspaces", "both", None])
@pytest.mark.parametrize("claim", [None, "jwt-user"])
def test_device_login_saves_before_independent_enrichment(account, monkeypatch, failed, claim):
    from contextlib import contextmanager
    import requests

    locked = []
    events = []

    @contextmanager
    def lock(alias):
        assert alias == account
        locked.append(True)
        events.append("lock")
        try:
            yield
        finally:
            locked.pop()

    access = jwt_for(claim) if claim else "opaque-access"

    def authorize(self, notify):
        assert not locked
        events.append("authorize")
        return TokenResponse(access, "fresh-refresh", time.time() + 3600, time.time() + 6000)

    def send(http, prepared, **kwargs):
        assert not locked
        cached = WebSession.load(account=account)
        assert cached.access_token == access
        assert cached.refresh_token == "fresh-refresh"
        kind = "detail" if "GetUserDetail" in prepared.url else "workspaces"
        events.append(kind)
        if failed in {kind, "both"}:
            raise requests.ConnectionError("private-token-must-not-be-logged")
        payload = (
            {"Result": {"id": "user-id", "extra_info": {"login_name": "platform-user"}}}
            if kind == "detail" else {"Result": {"items": []}}
        )
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(auth, "exclusive_session_refresh", lock)
    monkeypatch.setattr(auth.DeviceAuthorization, "login", authorize)
    monkeypatch.setattr(requests.Session, "send", send)
    result = auth.device_login(account, lambda *args: None)
    assert events == ["authorize", "lock", "detail", "workspaces", "lock"]
    expected = claim or (None if failed in {"detail", "both"} else "platform-user")
    assert result.login_username == expected
    assert WebSession.load(account=account).login_username == expected


def test_load_repairs_and_persists_jwt_login_name(account):
    cached = replace(session(account), access_token=jwt_for("recovered-user"), login_username=None)
    cached.save()
    restored = WebSession.load(account=account)
    assert restored.login_username == "recovered-user"
    payload = json.loads((accounts.account_dir(account) / "web_session.json").read_text())
    assert payload["login_username"] == "recovered-user"


def test_notebook_route_preserves_other_origins(account, monkeypatch):
    from types import SimpleNamespace
    from inspire.platform.web.browser_api import playwright_notebooks as notebooks
    import playwright.sync_api

    context = Mock()
    context.new_page.return_value = Mock()
    browser = Mock()
    manager = Mock()
    manager.__enter__ = Mock(return_value=Mock())
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: manager)
    monkeypatch.setattr(notebooks, "_launch_browser", lambda *a, **kw: browser)
    monkeypatch.setattr(notebooks, "_new_context", lambda *a, **kw: context)
    monkeypatch.setattr(notebooks, "_get_base_url", lambda: "https://platform.test")
    monkeypatch.setattr(notebooks, "_find_ide_gateway_url", lambda page: "https://notebook.test")
    notebooks._resolve_notebook_ide_url_sync("notebook", session=session(account))
    handler = context.route.call_args.args[1]
    for url, platform in [("https://platform.test/api", True), ("https://notebook.test/api", False)]:
        headers = {"Authorization": "Basic application-auth", "X-Inspire-Client-Source": "own"}
        route = SimpleNamespace(request=SimpleNamespace(url=url, headers=headers), continue_=Mock())
        handler(route)
        if platform:
            result = route.continue_.call_args.kwargs["headers"]
            assert result["Authorization"] == "Bearer old-access"
        else:
            route.continue_.assert_called_once_with()
            assert headers["Authorization"] == "Basic application-auth"


def test_require_workspace_discovers_and_persists_catalog(account, monkeypatch):
    session(account).save()

    def discover(target, base_url):
        assert base_url == "https://platform.test"
        target.workspace_id = "ws-real"
        target.all_workspace_ids = ["ws-real"]
        target.all_workspace_names = {"ws-real": "Real"}

    monkeypatch.setattr(auth, "_discover_identity", discover)
    assert auth.get_web_session(require_workspace=True).workspace_id == "ws-real"
    assert WebSession.load(account=account).workspace_id == "ws-real"


def test_cached_catalog_skips_discovery(account, monkeypatch):
    cached = session(account)
    cached.workspace_id = "ws-real"
    cached.save()
    monkeypatch.setattr(auth, "_discover_identity", Mock(side_effect=AssertionError("discovered")))
    assert auth.get_web_session(require_workspace=True).workspace_id == "ws-real"
