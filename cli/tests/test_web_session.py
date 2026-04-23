import json
import threading
import time
from pathlib import Path

import pytest
import requests

from inspire.config import Config
from inspire.platform.web import session as ws
from inspire.platform.web.session import auth as ws_auth
from inspire.platform.web.session import browser_client as ws_browser_client
from inspire.platform.web.session import WebSession
from inspire.platform.web.session import requests as ws_requests_module


class DummyResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class DummyHTTP:
    def __init__(self, response: DummyResponse) -> None:
        self.response = response
        self.calls = []

    def get(self, url, headers=None, timeout=None):  # noqa: ANN001
        self.calls.append(("GET", url, headers, timeout))
        return self.response

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: ANN001
        self.calls.append(("POST", url, headers, json, timeout))
        return self.response

    def delete(self, url, headers=None, timeout=None):  # noqa: ANN001
        self.calls.append(("DELETE", url, headers, timeout))
        return self.response

    def close(self) -> None:
        pass


class DummyBrowserClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request_json(self, method, url, headers=None, body=None, timeout=30):  # noqa: ANN001
        self.calls.append((method, url, headers, body, timeout))
        return self.payload


class DummyAPIResponse:
    def __init__(self, status: int = 200, payload=None) -> None:
        self.status = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class DummyRequestContext:
    def __init__(self) -> None:
        self.calls = []

    def get(self, url, headers=None, timeout=None):  # noqa: ANN001
        self.calls.append(("GET", url, headers, None, timeout))
        return DummyAPIResponse(200, {"ok": True})

    def post(self, url, headers=None, data=None, timeout=None):  # noqa: ANN001
        self.calls.append(("POST", url, headers, data, timeout))
        return DummyAPIResponse(200, {"ok": True})

    def delete(self, url, headers=None, timeout=None):  # noqa: ANN001
        self.calls.append(("DELETE", url, headers, None, timeout))
        return DummyAPIResponse(200, {"ok": True})


class DummyBrowserContext:
    def __init__(self) -> None:
        self.request = DummyRequestContext()


def test_build_requests_session_applies_toml_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )
    monkeypatch.setattr(
        ws_requests_module,
        "resolve_requests_proxy_config",
        lambda: (
            {
                "http": "http://127.0.0.1:7897",
                "https": "http://127.0.0.1:7897",
            },
            "toml",
        ),
    )

    http = ws_requests_module.build_requests_session(session, "https://qz.sii.edu.cn/api/v1/test")

    assert http.proxies["http"] == "http://127.0.0.1:7897"
    assert http.proxies["https"] == "http://127.0.0.1:7897"
    assert http.trust_env is False
    http.close()


def test_request_json_falls_back_to_browser_client(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    http = DummyHTTP(DummyResponse(401))
    browser = DummyBrowserClient({"ok": True})

    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws, "_get_browser_client", lambda _session: browser)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", False)

    result = ws.request_json(session, "GET", "https://example.test")

    assert result == {"ok": True}
    assert ws._BROWSER_API_FORCE_BROWSER is True
    assert http.calls
    assert browser.calls


def test_request_json_non_json_triggers_fallback(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    http = DummyHTTP(DummyResponse(200, payload=ValueError("bad json")))
    browser = DummyBrowserClient({"ok": True})

    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws, "_get_browser_client", lambda _session: browser)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", False)

    result = ws.request_json(session, "GET", "https://example.test")

    assert result == {"ok": True}
    assert ws._BROWSER_API_FORCE_BROWSER is True
    assert http.calls
    assert browser.calls


def test_request_json_transport_error_triggers_fallback(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    class FailingHTTP:
        def __init__(self) -> None:
            self.calls = []

        def get(self, url, headers=None, timeout=None):  # noqa: ANN001
            self.calls.append(("GET", url, headers, timeout))
            raise requests.exceptions.SSLError("ssl eof")

        def close(self) -> None:
            pass

    http = FailingHTTP()
    browser = DummyBrowserClient({"ok": True})

    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws, "_get_browser_client", lambda _session: browser)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", False)

    result = ws.request_json(session, "GET", "https://example.test")

    assert result == {"ok": True}
    assert ws._BROWSER_API_FORCE_BROWSER is True
    assert http.calls
    assert browser.calls


def test_request_json_supports_delete(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    http = DummyHTTP(DummyResponse(200, payload={"ok": True}))

    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", False)

    result = ws.request_json(session, "DELETE", "https://example.test/api/v1/image/image-1")

    assert result == {"ok": True}
    assert http.calls == [("DELETE", "https://example.test/api/v1/image/image-1", {}, 30)]


def test_browser_client_reset_on_expired(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    class ExpiringBrowserClient:
        def request_json(self, *_args, **_kwargs):
            raise ws.SessionExpiredError("expired")

    closed = {"called": False}

    def fake_close() -> None:
        closed["called"] = True

    def fake_get_web_session(**_kwargs):
        # Simulate re-authentication failure by raising SessionExpiredError
        raise ws.SessionExpiredError("re-auth failed")

    monkeypatch.setattr(ws, "_get_browser_client", lambda _session: ExpiringBrowserClient())
    monkeypatch.setattr(ws, "_close_browser_client", fake_close)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", True)
    monkeypatch.setattr(ws, "get_web_session", fake_get_web_session)

    with pytest.raises(ws.SessionExpiredError):
        ws.request_json(session, "GET", "https://example.test")

    assert closed["called"] is True


def test_request_json_reauth_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )
    refreshed = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "new"}]},
        cookies={"session": "new"},
        workspace_id="ws-test",
        created_at=1,
    )

    class ExpiringBrowserClient:
        def request_json(self, *_args, **_kwargs):
            raise ws.SessionExpiredError("expired")

    monkeypatch.setattr(ws, "_get_browser_client", lambda _session: ExpiringBrowserClient())
    monkeypatch.setattr(ws, "_close_browser_client", lambda: None)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", True)
    monkeypatch.setattr(ws, "clear_session_cache", lambda: None)
    monkeypatch.setattr(ws, "get_web_session", lambda **_kwargs: refreshed)

    with pytest.raises(ws.SessionExpiredError):
        ws.request_json(session, "GET", "https://example.test")

    captured = capsys.readouterr()
    assert "Session expired, re-authenticating..." not in captured.err


def test_request_json_reauth_refreshes_session_in_place(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "old"}]},
        cookies={"session": "old"},
        workspace_id="ws-old",
        login_username="old-user",
        created_at=1.0,
    )
    refreshed = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "new"}]},
        cookies={"session": "new"},
        workspace_id="ws-new",
        login_username="new-user",
        created_at=2.0,
    )

    class ExpiringBrowserClient:
        def request_json(self, *_args, **_kwargs):
            raise ws.SessionExpiredError("expired")

    class WorkingBrowserClient:
        def __init__(self) -> None:
            self.calls = 0

        def request_json(self, *_args, **_kwargs):
            self.calls += 1
            return {"ok": True}

    working = WorkingBrowserClient()
    refresh_calls = {"count": 0}

    def fake_get_browser_client(current_session: WebSession):  # type: ignore[no-untyped-def]
        cookie_value = current_session.storage_state.get("cookies", [{}])[0].get(
            "value"
        )  # type: ignore[index]
        if cookie_value == "old":
            return ExpiringBrowserClient()
        return working

    def fake_get_web_session(**_kwargs):
        refresh_calls["count"] += 1
        return refreshed

    monkeypatch.setattr(ws, "_get_browser_client", fake_get_browser_client)
    monkeypatch.setattr(ws, "_close_browser_client", lambda: None)
    monkeypatch.setattr(ws, "clear_session_cache", lambda: None)
    monkeypatch.setattr(ws, "get_web_session", fake_get_web_session)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", True)

    result = ws.request_json(session, "GET", "https://example.test")
    assert result == {"ok": True}
    assert refresh_calls["count"] == 1
    assert session.storage_state == refreshed.storage_state
    assert session.cookies == refreshed.cookies
    assert session.workspace_id == refreshed.workspace_id
    assert session.login_username == refreshed.login_username
    assert session.created_at == refreshed.created_at
    assert working.calls == 1

    second_result = ws.request_json(session, "GET", "https://example.test")
    assert second_result == {"ok": True}
    assert refresh_calls["count"] == 1
    assert working.calls == 2


def test_browser_request_context_posts_json_bytes():
    client = ws._BrowserRequestClient.__new__(ws._BrowserRequestClient)
    context = DummyBrowserContext()
    client._context = context
    client._closed = False
    client.session_fingerprint = "test"

    result = client.request_json("POST", "https://example.test", body={"a": 1})

    assert result == {"ok": True}
    assert context.request.calls
    method, _url, headers, data, _timeout = context.request.calls[0]
    assert method == "POST"
    assert json.loads(data) == {"a": 1}
    header_keys = {key.lower() for key in (headers or {})}
    assert "content-type" in header_keys


def test_browser_request_context_supports_delete():
    client = ws._BrowserRequestClient.__new__(ws._BrowserRequestClient)
    context = DummyBrowserContext()
    client._context = context
    client._closed = False
    client.session_fingerprint = "test"

    result = client.request_json("DELETE", "https://example.test/api/v1/image/image-1")

    assert result == {"ok": True}
    assert context.request.calls
    method, _url, _headers, _data, _timeout = context.request.calls[0]
    assert method == "DELETE"


def test_browser_client_cache_is_thread_local(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    created: list["FakeBrowserClient"] = []

    class FakeBrowserClient:
        def __init__(self, current_session: WebSession) -> None:
            self.session_fingerprint = ws_browser_client._session_fingerprint(current_session)
            self.created_thread = threading.get_ident()
            self._closed = False
            created.append(self)

        def close(self) -> None:
            self._closed = True

    monkeypatch.setattr(ws_browser_client, "_BrowserRequestClient", FakeBrowserClient)
    ws_browser_client._close_browser_client()

    main_client_1 = ws_browser_client._get_browser_client(session)
    main_client_2 = ws_browser_client._get_browser_client(session)

    worker: dict[str, object] = {}

    def _worker() -> None:
        worker_client_1 = ws_browser_client._get_browser_client(session)
        worker_client_2 = ws_browser_client._get_browser_client(session)
        worker["client"] = worker_client_1
        worker["same"] = worker_client_1 is worker_client_2

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()

    assert main_client_1 is main_client_2
    assert worker["same"] is True
    assert worker["client"] is not main_client_1
    assert len(created) == 2

    ws_browser_client._close_browser_client()
    assert all(client._closed for client in created)


def test_browser_client_recreates_closed_thread_local_client(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    created: list["FakeBrowserClient"] = []

    class FakeBrowserClient:
        def __init__(self, current_session: WebSession) -> None:
            self.session_fingerprint = ws_browser_client._session_fingerprint(current_session)
            self._closed = False
            created.append(self)

        def close(self) -> None:
            self._closed = True

    monkeypatch.setattr(ws_browser_client, "_BrowserRequestClient", FakeBrowserClient)
    ws_browser_client._close_browser_client()

    ready = threading.Event()
    proceed = threading.Event()
    done = threading.Event()
    result: dict[str, object] = {}

    def _worker() -> None:
        first = ws_browser_client._get_browser_client(session)
        result["first"] = first
        ready.set()
        assert proceed.wait(timeout=2.0)
        second = ws_browser_client._get_browser_client(session)
        result["second"] = second
        done.set()

    thread = threading.Thread(target=_worker)
    thread.start()
    assert ready.wait(timeout=2.0)

    ws_browser_client._close_browser_client()
    proceed.set()
    assert done.wait(timeout=2.0)
    thread.join(timeout=2.0)

    first = result["first"]
    second = result["second"]

    assert first is not second
    assert getattr(first, "_closed", False) is True
    assert getattr(second, "_closed", False) is False

    ws_browser_client._close_browser_client()


def test_get_credentials_prefers_account_toml_when_prefer_source_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """prefer_source='toml' in project config makes account TOML password
    win over INSPIRE_PASSWORD env var. Account TOML is now the sole
    identity source."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    account_dir = fake_home / ".inspire" / "accounts" / "alice"
    account_dir.mkdir(parents=True)
    (account_dir / "config.toml").write_text(
        '[auth]\npassword = "account-pass"\n'
    )
    (fake_home / ".inspire" / "current").write_text("alice\n")

    project_dir = tmp_path / ".inspire"
    project_dir.mkdir()
    (project_dir / "config.toml").write_text(
        '[cli]\nprefer_source = "toml"\n\n'
        '[auth]\nusername = "toml-user"\n'
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INSPIRE_USERNAME", "env-user")
    monkeypatch.setenv("INSPIRE_PASSWORD", "env-pass")

    username, password = ws.get_credentials()

    assert username == "toml-user"
    assert password == "account-pass"


def test_get_web_session_reauths_when_cached_user_mismatch(monkeypatch: pytest.MonkeyPatch):
    cached = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        login_username="old-user",
        created_at=0,
    )
    refreshed = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "new"}]},
        cookies={"session": "new"},
        workspace_id="ws-test",
        login_username="new-user",
        created_at=1,
    )
    calls: dict[str, str] = {}

    monkeypatch.setattr(
        ws_auth.WebSession,
        "load",
        classmethod(lambda cls, allow_expired=False, account=None: cached),
    )
    monkeypatch.setattr(ws_auth, "get_credentials", lambda: ("new-user", "new-pass"))
    monkeypatch.setattr(
        ws_auth,
        "_load_runtime_config",
        lambda: type("Cfg", (), {"base_url": "https://example.invalid"})(),
    )

    def fake_login(username: str, password: str, base_url: str = "", headless: bool = True):
        calls["username"] = username
        calls["password"] = password
        calls["base_url"] = base_url
        calls["headless"] = str(headless)
        return refreshed

    monkeypatch.setattr(ws_auth, "login_with_playwright", fake_login)

    session = ws_auth.get_web_session(force_refresh=False, require_workspace=False)

    assert session is refreshed
    assert calls["username"] == "new-user"
    assert calls["password"] == "new-pass"
    assert calls["base_url"] == "https://example.invalid"


def test_get_web_session_reads_cached_session_without_explicit_account(
    monkeypatch: pytest.MonkeyPatch,
):
    """``get_web_session`` no longer threads the login-username through as a
    cache key — resolution lives inside ``WebSession.load`` via the active
    InspireSkill account (``~/.inspire/current``)."""
    cached = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        login_username="project-user",
        created_at=0,
    )
    load_calls: list[str | None] = []

    def fake_load(cls, allow_expired=False, account=None):  # type: ignore[no-untyped-def]
        load_calls.append(account)
        return cached

    monkeypatch.setattr(ws_auth.WebSession, "load", classmethod(fake_load))
    monkeypatch.setattr(ws_auth, "get_credentials", lambda: ("project-user", "secret"))

    session = ws_auth.get_web_session(force_refresh=False, require_workspace=False)

    assert session is cached
    assert load_calls
    # Caller passes no explicit account; internal resolution handles it.
    assert load_calls[0] is None


def test_get_web_session_force_refresh_bypasses_cache(monkeypatch: pytest.MonkeyPatch):
    cached = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "old"}]},
        cookies={"session": "old"},
        workspace_id="ws-old",
        login_username="refresh-user",
        created_at=0,
    )
    refreshed = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "new"}]},
        cookies={"session": "new"},
        workspace_id="ws-new",
        login_username="refresh-user",
        created_at=1,
    )
    load_calls: list[tuple[bool, str | None]] = []
    login_calls: dict[str, str] = {}

    def fake_load(cls, allow_expired=False, account=None):  # type: ignore[no-untyped-def]
        load_calls.append((allow_expired, account))
        return cached

    def fake_login(username: str, password: str, base_url: str = "", headless: bool = True):
        login_calls["username"] = username
        login_calls["password"] = password
        login_calls["base_url"] = base_url
        login_calls["headless"] = str(headless)
        return refreshed

    monkeypatch.setattr(ws_auth.WebSession, "load", classmethod(fake_load))
    monkeypatch.setattr(ws_auth, "get_credentials", lambda: ("refresh-user", "refresh-pass"))
    monkeypatch.setattr(
        ws_auth,
        "_load_runtime_config",
        lambda: type("Cfg", (), {"base_url": "https://example.invalid"})(),
    )
    monkeypatch.setattr(ws_auth, "login_with_playwright", fake_login)

    session = ws_auth.get_web_session(force_refresh=True, require_workspace=False)

    assert session is refreshed
    assert load_calls == []
    assert login_calls["username"] == "refresh-user"
    assert login_calls["password"] == "refresh-pass"
    assert login_calls["base_url"] == "https://example.invalid"


def test_asyncio_browser_fallback_uses_disposable_clients(monkeypatch: pytest.MonkeyPatch):
    """Two consecutive browser-backed requests from an asyncio context must each
    get their own disposable _BrowserRequestClient — not the global cached one —
    to avoid cross-thread greenlet / thread-affinity errors.
    """
    import asyncio
    import threading

    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    created: list = []

    class TrackedClient:
        def __init__(self, _session):
            self.thread_id = threading.current_thread().ident
            self.closed = False
            created.append(self)

        def request_json(self, method, url, headers=None, body=None, timeout=30):
            # Core assertion: client used on the same thread that created it
            assert threading.current_thread().ident == self.thread_id
            return {"ok": True}

        def close(self):
            self.closed = True

    def _fail_global_cache(_session):
        raise AssertionError("global cache must not be used in asyncio path")

    monkeypatch.setattr(ws, "_BrowserRequestClient", TrackedClient)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", True)
    monkeypatch.setattr(ws, "_get_browser_client", _fail_global_cache)

    async def two_requests():
        r1 = ws.request_json(session, "GET", "https://example.test/1")
        r2 = ws.request_json(session, "GET", "https://example.test/2")
        return r1, r2

    r1, r2 = asyncio.run(two_requests())

    assert r1 == {"ok": True}
    assert r2 == {"ok": True}
    assert len(created) == 2, "each call should create its own client"
    assert created[0] is not created[1], "clients must not be reused"
    assert all(c.closed for c in created), "disposable clients must be closed"


def test_clear_session_cache_removes_every_account_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    accounts_root = fake_home / ".inspire" / "accounts"
    (accounts_root / "alice").mkdir(parents=True)
    (accounts_root / "alice" / "web_session.json").write_text("{}")
    (accounts_root / "bob").mkdir()
    (accounts_root / "bob" / "web_session.json").write_text("{}")
    (accounts_root / "bob" / "config.toml").write_text("")  # unrelated file kept

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    ws.clear_session_cache()

    assert not (accounts_root / "alice" / "web_session.json").exists()
    assert not (accounts_root / "bob" / "web_session.json").exists()
    assert (accounts_root / "bob" / "config.toml").exists()


# --- Phase 3: account-scoped session storage -----------------------------


def test_get_session_cache_file_prefers_account_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from inspire.platform.web.session.models import get_session_cache_file

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    path = get_session_cache_file("alice")
    assert path == fake_home / ".inspire" / "accounts" / "alice" / "web_session.json"


def test_save_writes_to_account_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    import inspire.accounts as accounts_mod

    monkeypatch.setattr(accounts_mod, "current_account", lambda: "alice")

    session = WebSession(
        storage_state={"cookies": []},
        cookies={},
        login_username="platform-user",
        created_at=time.time(),
    )
    session.save()

    target = fake_home / ".inspire" / "accounts" / "alice" / "web_session.json"
    assert target.exists()


def test_load_env_vars_do_not_influence_account_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    import inspire.accounts as accounts_mod

    monkeypatch.setattr(accounts_mod, "current_account", lambda: None)
    monkeypatch.setenv("INSPIRE_USERNAME", "ghost")
    monkeypatch.setenv("INSPIRE_ACCOUNT", "ghost")
    monkeypatch.setenv("INSPIRE_BRIDGE_ACCOUNT", "ghost")

    from inspire.platform.web.session.models import get_session_cache_file

    path = get_session_cache_file()
    # Must NOT resolve to anything under accounts/ghost/...
    assert "accounts/ghost" not in str(path)
