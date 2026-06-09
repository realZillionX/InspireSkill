import json
import threading
import time
from pathlib import Path

import pytest
import requests

from inspire.platform.web import session as ws
from inspire.platform.web.session import auth as ws_auth
from inspire.platform.web.session import browser_launch
from inspire.platform.web.session.browser_launch import (
    CHROMIUM_CHANNEL_ENV,
    CHROMIUM_CONTAINER_ARGS,
    CHROMIUM_EXECUTABLE_ENV,
    chromium_launch_kwargs,
)
from inspire.platform.web.session import browser_client as ws_browser_client
from inspire.platform.web.session import WebSession
from inspire.platform.web.session import requests as ws_requests_module

CAS_RSA_EXPONENT = "010001"
CAS_RSA_MODULUS = (
    "008aed7e057fe8f14c73550b0e6467b023616ddc8fa91846d2613cdb7f7621e3cada4cd5"
    "d812d627af6b87727ade4e26d26208b7326815941492b2204c3167ab2d53df1e3a2c"
    "9153bdb7c8c2e968df97a5e7e01cc410f92c4c2c2fba529b3ee988ebc1fca99ff"
    "5119e036d732c368acf8beba01aa2fdafa45b21e4de4928d0d403"
)


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


def test_chromium_launch_kwargs_include_container_compat_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CHROMIUM_EXECUTABLE_ENV, raising=False)
    monkeypatch.delenv(CHROMIUM_CHANNEL_ENV, raising=False)
    proxy = {"server": "http://127.0.0.1:7897"}

    kwargs = chromium_launch_kwargs(headless=True, proxy=proxy)

    assert kwargs["headless"] is True
    assert kwargs["proxy"] == proxy
    for arg in CHROMIUM_CONTAINER_ARGS:
        assert arg in kwargs["args"]


def test_chromium_launch_kwargs_uses_executable_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CHROMIUM_EXECUTABLE_ENV, "  /opt/google/chrome/chrome  ")
    monkeypatch.setenv(CHROMIUM_CHANNEL_ENV, "chrome")

    kwargs = chromium_launch_kwargs(headless=True)

    assert kwargs["executable_path"] == "/opt/google/chrome/chrome"
    assert "channel" not in kwargs


def test_chromium_launch_kwargs_uses_channel_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CHROMIUM_EXECUTABLE_ENV, raising=False)
    monkeypatch.setenv(CHROMIUM_CHANNEL_ENV, "  chrome  ")

    kwargs = chromium_launch_kwargs(headless=True)

    assert kwargs["channel"] == "chrome"
    assert "executable_path" not in kwargs


def test_browser_closed_error_detection() -> None:
    assert ws_auth._is_browser_closed_error(
        RuntimeError("Page.goto: Target page, context or browser has been closed")
    )
    assert not ws_auth._is_browser_closed_error(RuntimeError("Timeout 60000ms exceeded"))


def test_browser_launch_runtime_error_detection() -> None:
    assert ws_auth._is_browser_launch_runtime_error(
        RuntimeError(
            "BrowserType.launch: Target page, context or browser has been closed\n"
            "error while loading shared libraries: libglib-2.0.so.0"
        )
    )
    assert not ws_auth._is_browser_launch_runtime_error(RuntimeError("Timeout 60000ms exceeded"))


def test_workspace_routes_from_payload_extracts_workspace_list() -> None:
    payload = {
        "data": {
            "routes": [
                {"name": "operations", "routes": [{"path": "not-a-workspace"}]},
                {
                    "name": "userWorkspaceList",
                    "routes": [
                        {"name": "CPU资源空间", "path": "ws-11111111-1111-1111-1111-111111111111"},
                        {"name": "invalid", "path": "default"},
                        {
                            "name": "分布式训练空间",
                            "path": "ws-22222222-2222-2222-2222-222222222222",
                        },
                    ],
                },
            ]
        }
    }

    ids, names = ws_auth._workspace_routes_from_payload(payload)

    assert ids == [
        "ws-11111111-1111-1111-1111-111111111111",
        "ws-22222222-2222-2222-2222-222222222222",
    ]
    assert names == {
        "ws-11111111-1111-1111-1111-111111111111": "CPU资源空间",
        "ws-22222222-2222-2222-2222-222222222222": "分布式训练空间",
    }


def test_legacy_cas_encrypt_password_matches_page_rsa() -> None:
    assert (
        ws_auth._legacy_cas_encrypt_password("abc", CAS_RSA_EXPONENT, CAS_RSA_MODULUS)
        == "050d4541820093722eb891339242b9e3147ba98618ed03dd97dc98f4719b0a76"
        "a139138a7087ca84ee933dc56d7e7fa615a2dbcd4cda0f356eabedd98616a7a5"
        "cb06926a5005f4c1fe367725e3c0d4651889c92eec7912eb6b01e8edc342acb5"
        "bb11bd05b8bbd51cb4111954df11bcaf2b904c6eabddf6e1a881d57d95490cd5"
    )
    assert (
        ws_auth._legacy_cas_encrypt_password("password123", CAS_RSA_EXPONENT, CAS_RSA_MODULUS)
        == "33547d9dd849975180b45e4bb2377dff321cafb69206403602c31078746174ab"
        "0fd67a3749053d7c864302da7a4603fffca1247ce45d18b5a8f4944692541c409"
        "8f6fe4e0bbd25218c7cc55c5b51d7dc4fb89b66d96cdace1581f09d39ade57b"
        "f83aa7d40bcf1ecff98e92bab666ad3c38808bf83e09eeaa792fa3bf83f1ecc6"
    )


def test_extract_login_form_picks_cas_password_form() -> None:
    html = """
    <form id="fm2" action="/sms"><input name="username"><input name="smscode"></form>
    <form id="fm1" action="/cas/login?service=x">
      <input name="username" value="">
      <input id="passwordShow" type="password">
      <input id="password" name="password" type="hidden">
      <input name="execution" value="exec-1">
      <input name="_eventId" value="submit">
      <input name="loginType" value="1">
    </form>
    """

    action, fields = ws_auth._extract_login_form(html, "https://cas.sii.edu.cn/cas/login")

    assert action == "https://cas.sii.edu.cn/cas/login?service=x"
    assert fields["username"] == ""
    assert fields["password"] == ""
    assert fields["execution"] == "exec-1"
    assert fields["_eventId"] == "submit"
    assert fields["loginType"] == "1"


def test_extract_cas_rsa_key_from_login_js() -> None:
    text = f"""
    RSAUtils.setMaxDigits(131);
    var key = RSAUtils.getKeyPair("{CAS_RSA_EXPONENT}", '', "{CAS_RSA_MODULUS}");
    var result = RSAUtils.encryptedString(key, password);
    """

    assert ws_auth._extract_cas_rsa_key(text) == (CAS_RSA_EXPONENT, CAS_RSA_MODULUS)


def test_resolve_cas_rsa_key_reads_same_origin_script() -> None:
    class Response:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            pass

    class HTTP:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def get(self, url: str, timeout: int) -> Response:
            self.urls.append(url)
            return Response(
                f'var key = RSAUtils.getKeyPair("{CAS_RSA_EXPONENT}", "", "{CAS_RSA_MODULUS}");'
            )

    html = """
    <script src="https://cdn.example.com/ignored.js"></script>
    <script src="/cas/themes/sudy_fudan_ai/js/login.js"></script>
    """
    http = HTTP()

    assert ws_auth._resolve_cas_rsa_key(http, html, "https://cas.sii.edu.cn/cas/login") == (
        CAS_RSA_EXPONENT,
        CAS_RSA_MODULUS,
    )
    assert http.urls == ["https://cas.sii.edu.cn/cas/themes/sudy_fudan_ai/js/login.js"]


def test_decode_keycloak_login_url() -> None:
    html = '"loginUrl": "\\/realms\\/inf-internal\\/broker\\/cas\\/login?client_id=x"'

    url = ws_auth._decode_keycloak_login_url(
        html,
        "https://keycloak-inspire-prod.sii.edu.cn/realms/inf-internal/login",
    )

    assert (
        url
        == "https://keycloak-inspire-prod.sii.edu.cn/realms/inf-internal/broker/cas/login?client_id=x"
    )


def test_playwright_install_args_include_deps_for_root_linux_apt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_launch.sys, "platform", "linux")
    monkeypatch.setattr(browser_launch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        browser_launch.shutil,
        "which",
        lambda name: "/usr/bin/apt-get" if name == "apt-get" else None,
    )

    assert browser_launch.playwright_install_args() == [
        "install",
        "--with-deps",
        "chromium",
    ]


def test_playwright_install_args_skip_deps_when_not_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_launch.sys, "platform", "linux")
    monkeypatch.setattr(browser_launch.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        browser_launch.shutil,
        "which",
        lambda name: "/usr/bin/apt-get" if name == "apt-get" else None,
    )

    assert browser_launch.playwright_install_args() == ["install", "chromium"]


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


def test_request_json_browser_runtime_error_uses_standard_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    def raise_browser_runtime_error(_session):  # noqa: ANN001
        raise RuntimeError(
            "BrowserType.launch: Executable doesn't exist at /tmp/chromium\n"
            "Please run the following command to download new browsers:\n"
            "    playwright install"
        )

    monkeypatch.setattr(ws, "_get_browser_client", raise_browser_runtime_error)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", True)

    with pytest.raises(RuntimeError) as excinfo:
        ws.request_json(session, "GET", "https://example.test")

    message = str(excinfo.value)
    assert "inspire update --cli-only" in message
    assert "playwright install" not in message


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
    monkeypatch.setattr(ws, "clear_session_cache", lambda **_kwargs: None)
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
        cookie_value = current_session.storage_state.get("cookies", [{}])[0].get("value")  # type: ignore[index]
        if cookie_value == "old":
            return ExpiringBrowserClient()
        return working

    def fake_get_web_session(**_kwargs):
        refresh_calls["count"] += 1
        return refreshed

    monkeypatch.setattr(ws, "_get_browser_client", fake_get_browser_client)
    monkeypatch.setattr(ws, "_close_browser_client", lambda: None)
    monkeypatch.setattr(ws, "clear_session_cache", lambda **_kwargs: None)
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


def test_close_browser_client_does_not_block_on_hung_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )
    started = threading.Event()
    release = threading.Event()

    class HangingBrowserClient:
        def __init__(self, current_session: WebSession) -> None:
            self.session_fingerprint = ws_browser_client._session_fingerprint(current_session)
            self._closed = False

        def close(self) -> None:
            self._closed = True
            started.set()
            release.wait(timeout=5)

    monkeypatch.setattr(ws_browser_client, "_BrowserRequestClient", HangingBrowserClient)
    monkeypatch.setattr(ws_browser_client, "_BROWSER_CLIENT_CLOSE_TIMEOUT_SECONDS", 0.01)
    ws_browser_client._close_browser_client()

    client = ws_browser_client._get_browser_client(session)
    start = time.monotonic()
    ws_browser_client._close_browser_client()
    elapsed = time.monotonic() - start
    release.set()

    assert started.wait(timeout=1)
    assert elapsed < 0.5
    assert client._closed is True


def test_get_credentials_reads_account_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """v4.0.0: account TOML is the sole identity source.

    Identity (`[auth]`) is account-scope and cannot live in the project
    layer at all — the loader rejects it. With env unset, get_credentials
    returns the active account's stored values.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    account_dir = fake_home / ".inspire" / "accounts" / "alice"
    account_dir.mkdir(parents=True)
    (account_dir / "config.toml").write_text(
        '[auth]\nusername = "account-user"\npassword = "account-pass"\n'
    )
    (fake_home / ".inspire" / "current").write_text("alice\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("INSPIRE_USERNAME", raising=False)
    monkeypatch.delenv("INSPIRE_PASSWORD", raising=False)

    username, password = ws.get_credentials()

    assert username == "account-user"
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
    monkeypatch.setattr(ws_auth, "get_credentials", lambda account=None: ("new-user", "new-pass"))
    monkeypatch.setattr(
        ws_auth,
        "_load_runtime_config",
        lambda account=None: type("Cfg", (), {"base_url": "https://example.invalid"})(),
    )

    def fake_login(
        username: str,
        password: str,
        base_url: str = "",
        headless: bool = True,
        account=None,  # noqa: ANN001
    ):
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
    monkeypatch.setattr(ws_auth, "get_credentials", lambda account=None: ("project-user", "secret"))

    session = ws_auth.get_web_session(force_refresh=False, require_workspace=False)

    assert session is cached
    assert load_calls
    # Caller passes no explicit account; internal resolution handles it.
    assert load_calls[0] is None


def test_get_web_session_explicit_account_uses_account_cache_and_login(
    monkeypatch: pytest.MonkeyPatch,
):
    refreshed = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "new"}]},
        cookies={"session": "new"},
        workspace_id="ws-new",
        login_username="alice-user",
        created_at=1,
    )
    calls: dict[str, object] = {"loads": []}

    def fake_load(cls, allow_expired=False, account=None):  # type: ignore[no-untyped-def]
        calls["loads"].append((allow_expired, account))
        return None

    def fake_get_credentials(account=None):  # type: ignore[no-untyped-def]
        calls["credentials_account"] = account
        return "alice-user", "alice-pass"

    def fake_runtime_config(account=None):  # type: ignore[no-untyped-def]
        calls["runtime_account"] = account
        return type("Cfg", (), {"base_url": "https://alice.invalid"})()

    def fake_login(
        username: str,
        password: str,
        base_url: str = "",
        headless: bool = True,
        account=None,  # noqa: ANN001
    ):
        calls["login"] = {
            "username": username,
            "password": password,
            "base_url": base_url,
            "headless": headless,
            "account": account,
        }
        return refreshed

    monkeypatch.setattr(ws_auth.WebSession, "load", classmethod(fake_load))
    monkeypatch.setattr(ws_auth, "get_credentials", fake_get_credentials)
    monkeypatch.setattr(ws_auth, "_load_runtime_config", fake_runtime_config)
    monkeypatch.setattr(ws_auth, "login_with_playwright", fake_login)

    session = ws_auth.get_web_session(account="alice")

    assert session is refreshed
    assert calls["loads"] == [(False, "alice"), (True, "alice")]
    assert calls["credentials_account"] == "alice"
    assert calls["runtime_account"] == "alice"
    assert calls["login"]["account"] == "alice"


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

    def fake_login(
        username: str,
        password: str,
        base_url: str = "",
        headless: bool = True,
        account=None,  # noqa: ANN001
    ):
        login_calls["username"] = username
        login_calls["password"] = password
        login_calls["base_url"] = base_url
        login_calls["headless"] = str(headless)
        return refreshed

    monkeypatch.setattr(ws_auth.WebSession, "load", classmethod(fake_load))
    monkeypatch.setattr(
        ws_auth,
        "get_credentials",
        lambda account=None: ("refresh-user", "refresh-pass"),
    )
    monkeypatch.setattr(
        ws_auth,
        "_load_runtime_config",
        lambda account=None: type("Cfg", (), {"base_url": "https://example.invalid"})(),
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


def test_clear_session_cache_removes_only_active_account_cache(
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
    (fake_home / ".inspire" / "current").write_text("alice\n")

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    ws.clear_session_cache()

    assert not (accounts_root / "alice" / "web_session.json").exists()
    assert (accounts_root / "bob" / "web_session.json").exists()
    assert (accounts_root / "bob" / "config.toml").exists()


def test_clear_all_session_caches_removes_every_account_cache(
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
    ws.clear_all_session_caches()

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
