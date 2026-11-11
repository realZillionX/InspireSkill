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
WORKSPACE_ID = "ws-11111111-1111-1111-1111-111111111111"
WORKSPACE_ITEMS = [
    {"workspace_id": WORKSPACE_ID, "name": "Training", "is_fair_workspace": True}
]


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

    def get(self, url, headers=None, timeout=None, allow_redirects=True):  # noqa: ANN001
        assert allow_redirects is False
        self.calls.append(("GET", url, headers, timeout))
        return self.response

    def post(  # noqa: ANN001
        self, url, headers=None, json=None, timeout=None, allow_redirects=True
    ):
        assert allow_redirects is False
        self.calls.append(("POST", url, headers, json, timeout))
        return self.response

    def delete(self, url, headers=None, timeout=None, allow_redirects=True):  # noqa: ANN001
        assert allow_redirects is False
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
        self.headers = {}
        self._payload = payload or {}

    def json(self):
        return self._payload


class DummyRequestContext:
    def __init__(self) -> None:
        self.calls = []

    def get(self, url, headers=None, timeout=None, max_redirects=None):  # noqa: ANN001
        assert max_redirects == 0
        self.calls.append(("GET", url, headers, None, timeout))
        return DummyAPIResponse(200, {"ok": True})

    def post(  # noqa: ANN001
        self, url, headers=None, data=None, timeout=None, max_redirects=None
    ):
        assert max_redirects == 0
        self.calls.append(("POST", url, headers, data, timeout))
        return DummyAPIResponse(200, {"ok": True})

    def delete(self, url, headers=None, timeout=None, max_redirects=None):  # noqa: ANN001
        assert max_redirects == 0
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






def test_web_session_round_trip_preserves_workspace_capabilities() -> None:
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,
        workspace_id="ws-test",
        all_workspace_fair_scheduling={"ws-test": True, "ws-standard": False},
        created_at=1.0,
    )

    restored = WebSession.from_dict(session.to_dict())

    assert restored.all_workspace_fair_scheduling == {
        "ws-test": True,
        "ws-standard": False,
    }
    assert restored.access_token == "test-access"








# Reduced from pages captured off the live CAS on 2026-08-18. The whole point
# is the `style="display: none"`: every login tab is served hidden and the live
# one is chosen in JavaScript, so "is this container visible" is a question the
# served HTML cannot answer -- and answering it wrongly threw the platform's
# reason away.
_LIVE_CAS_TAB = (
    '<div class="content-box content-box-template-right" style="display: none">'
    '<section><form id="fm1"><div class="form-error">{slot}'
    '<span class="form-tab-nav" id="swiSpan1" name="swiSpan_fm1"></span>'
    "</div></form></section></div>"
)
_LIVE_CAS_CLEAN = _LIVE_CAS_TAB.format(slot="")
_LIVE_CAS_REJECTED = _LIVE_CAS_TAB.format(
    slot='<span id="msg1" name="error_fm1" class="form-tab-nav">账号或密码错误。</span>'
)






































class _FakePlaywrightLogin:
    """Browser I/O for failed authentication or successful catalog discovery."""

    def __init__(
        self, *, form_found: bool, page_html: str = "", workspace_payload: dict | None = None,
    ) -> None:
        self.form_found = form_found
        self.page_html = page_html
        self.workspace_payload = workspace_payload
        self.fills = 0
        self.submissions = 0
        self.closed = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        outer = self

        class Locator:
            def __init__(self) -> None:
                self.first = self

            def fill(self, _value: str) -> None:
                outer.fills += 1

            def press(self, _key: str, timeout: int) -> None:
                outer.submissions += 1

            def evaluate(self, _script: str) -> None:
                pass

        class Page:
            url = "https://cas.sii.edu.cn/login"

            def goto(self, *_args, **_kwargs) -> None:
                pass

            def wait_for_timeout(self, _timeout: int) -> None:
                pass

            def wait_for_selector(self, *_args, **_kwargs) -> None:
                if not outer.form_found:
                    raise RuntimeError("no login form on this page")

            def locator(self, _selector: str) -> Locator:
                return Locator()

            def get_by_text(self, *_args, **_kwargs) -> Locator:
                raise RuntimeError("no account-login tab either")

            def content(self) -> str:
                return outer.page_html

            def close(self) -> None:
                pass

        class Request:
            def post(self, url, **kwargs):
                from types import SimpleNamespace

                assert kwargs["data"] == {}
                if url.endswith("Action=GetUserDetail"):
                    payload = {"Result": {"id": "user-one"}}
                else:
                    assert url.endswith("Action=ListAccessibleWorkspaces")
                    payload = outer.workspace_payload
                return SimpleNamespace(status=200, json=lambda: payload)

        class Context:
            request = Request()

            def new_page(self) -> Page:
                return Page()

            def cookies(self):
                return [{"name": "inspire-session", "value": "authenticated"}]

            def storage_state(self):
                return {"cookies": self.cookies(), "origins": []}

        class Browser:
            def new_context(self, **_kwargs) -> Context:
                return Context()

            def close(self) -> None:
                outer.closed = True

        class Chromium:
            def launch(self, **_kwargs) -> Browser:
                return Browser()

        class PlaywrightContext:
            def __enter__(self):  # noqa: ANN204
                return type("Playwright", (), {"chromium": Chromium()})()

            def __exit__(self, *_args) -> None:
                pass

        monkeypatch.setattr(
            ws_auth,
            "_login_with_cas_requests",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("use browser")),
        )
        # A clock that jumps a thousand seconds per read, so `deadline =
        # time.time() + 30` is already in the past by the next read. Counting
        # reads instead would depend on how many the guard makes on the way in.
        reads = {"n": 0}

        def _clock() -> float:
            reads["n"] += 1
            return reads["n"] * 1000.0

        if outer.workspace_payload is None:
            monkeypatch.setattr(ws_auth.time, "time", _clock)
        monkeypatch.setattr(
            ws_auth, "resolve_playwright_proxy_config", lambda **_kwargs: ({}, "none")
        )
        monkeypatch.setattr(
            ws_auth,
            "describe_effective_proxy_config",
            lambda **_kwargs: {"playwright": {"route": "direct"}},
        )
        monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: PlaywrightContext())








class _RenewalResponse:
    def __init__(
        self,
        payload: dict | Exception | None = None,
        *,
        status_code: int = 200,
        text: str = "",
        url: str = "https://qz.sii.edu.cn/login",
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.url = url
        self._payload = payload if payload is not None else {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self) -> dict:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _RenewalHTTP(requests.Session):
    def __init__(
        self,
        user_response: _RenewalResponse,
        *,
        issue_platform_cookie: bool = True,
        workspace_response: _RenewalResponse | None = None,
    ) -> None:
        super().__init__()
        self.workspace_response = workspace_response or _RenewalResponse({"Result": {"items": []}})
        self.user_response = user_response
        self.issue_platform_cookie = issue_platform_cookie
        self.posts: list[str] = []
        self.seeded_cookie_names: set[str] = set()

    def get(self, url, **_kwargs):  # noqa: ANN001
        self.seeded_cookie_names = {cookie.name for cookie in self.cookies}
        if self.issue_platform_cookie:
            self.cookies.set(
                "inspire-session",
                "fresh",
                domain="qz.sii.edu.cn",
                path="/",
            )
        return _RenewalResponse()

    def post(self, url, **kwargs):  # noqa: ANN001
        self.posts.append(url)
        assert "password" not in kwargs.get("json", {})
        assert kwargs["allow_redirects"] is False
        if url.endswith("Action=GetUserDetail"):
            return self.user_response
        assert url.endswith("Action=ListAccessibleWorkspaces")
        assert kwargs["json"] == {}
        return self.workspace_response


def _expired_session_for_renewal(*, user_id: str | None = "user-one") -> WebSession:
    return WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,
        workspace_id="ws-old",
        user_detail={"id": user_id} if user_id else None,
        login_username="alice",
        base_url="https://qz.sii.edu.cn",
        account="alice",
        created_at=1.0,
    )










































def test_describe_proxy_config_redacts_credentials() -> None:
    assert __import__("inspire.platform.web.session.proxy", fromlist=["describe_proxy_config"]).describe_proxy_config(
        {
            "https": "http://user:secret@127.0.0.1:7897/private?token=value",
            "http": "http://127.0.0.1:7897",
        }
    ) == {
        "http": "http://127.0.0.1:7897",
        "https": "http://<redacted>@127.0.0.1:7897",
    }


class _StopCASProbe(RuntimeError):
    pass


class _RecordingRequestsSession(requests.Session):
    def __init__(self) -> None:
        super().__init__()
        self.request_url = ""
        self.effective_proxies: dict[str, str] = {}

    def get(self, url, *args, **kwargs):  # noqa: ANN001
        del args, kwargs
        self.request_url = url
        settings = self.merge_environment_settings(url, {}, None, None, None)
        self.effective_proxies = dict(settings["proxies"])
        raise _StopCASProbe


def _clear_standard_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
        "NO_PROXY",
        "no_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(key, raising=False)










def test_playwright_install_args_include_deps_for_root_linux_apt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_launch.sys, "platform", "linux")
    monkeypatch.setattr(browser_launch.os, "geteuid", lambda: 0, raising=False)
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
    monkeypatch.setattr(browser_launch.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        browser_launch.shutil,
        "which",
        lambda name: "/usr/bin/apt-get" if name == "apt-get" else None,
    )

    assert browser_launch.playwright_install_args() == ["install", "chromium"]


def test_playwright_install_hint_has_no_dependency_mode_argument() -> None:
    assert browser_launch.playwright_install_hint() == "inspire update --cli-only"


def test_build_requests_session_applies_toml_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

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

    http = ws_requests_module.build_requests_session(session, "https://qz.sii.edu.cn/api/v2/test")

    assert http.proxies["http"] == "http://127.0.0.1:7897"
    assert http.proxies["https"] == "http://127.0.0.1:7897"
    assert http.trust_env is False
    http.close()


def test_pooled_requests_session_keeps_one_connection_pool() -> None:
    # A fresh requests.Session per call hands the connection back after every
    # request, so a fan-out pays a TCP connect and TLS handshake per row it
    # reads. The pool has to outlive the call for keep-alive to mean anything.
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=0,
    )
    try:
        first = ws_requests_module.pooled_requests_session(session, "https://qz.sii.edu.cn")
        second = ws_requests_module.pooled_requests_session(session, "https://qz.sii.edu.cn")
        assert first is second
        assert ws_requests_module.build_requests_session(session, "https://qz.sii.edu.cn") is not first
    finally:
        ws_requests_module.close_pooled_requests_session()


def test_pooled_requests_sessions_are_isolated_between_threads() -> None:
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=0,
    )
    main_http = ws_requests_module.pooled_requests_session(
        session, "https://qz.sii.edu.cn"
    )
    worker: dict[str, object] = {}

    def load_twice() -> None:
        first = ws_requests_module.pooled_requests_session(
            session, "https://qz.sii.edu.cn"
        )
        second = ws_requests_module.pooled_requests_session(
            session, "https://qz.sii.edu.cn"
        )
        worker["first"] = first
        worker["same"] = first is second

    thread = threading.Thread(target=load_twice)
    thread.start()
    thread.join(timeout=5)
    try:
        assert worker["same"] is True
        assert worker["first"] is not main_http
    finally:
        ws_requests_module.close_pooled_requests_session()


def test_pooled_requests_session_never_answers_with_stale_cookies() -> None:
    # Only the pool is shared. A refreshed session's cookies must replace the
    # previous ones outright, not merge with them.
    first_session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=0,
    )
    refreshed = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=1,
    )
    try:
        http = ws_requests_module.pooled_requests_session(first_session, "https://qz.sii.edu.cn")
        http.cookies.set("drive-by", "from-a-response", domain="qz.sii.edu.cn", path="/")

        http = ws_requests_module.pooled_requests_session(refreshed, "https://qz.sii.edu.cn")

        assert not http.cookies
        assert http.prepare_request(requests.Request("GET", "https://qz.sii.edu.cn")).headers["Authorization"] == "Bearer test-access"
        assert http.cookies.get("drive-by", domain="qz.sii.edu.cn") is None
    finally:
        ws_requests_module.close_pooled_requests_session()


@pytest.mark.parametrize("status_code", [401, 302])
def test_request_json_auth_response_rebuilds_once_without_a_browser(
    transport,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    """401 and 302 both mean "not authenticated", and cost exactly one rebuild.

    Replaying the same refused cookies through Playwright first was pure
    latency, and it is the step that used to leave a browser transport pinned
    on for the rest of the process.
    """
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=1,
    )
    transport.adopt_session(session)
    refreshed = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=2,
    )

    expired_http = DummyHTTP(DummyResponse(status_code))
    fresh_http = DummyHTTP(DummyResponse(200, payload={"ok": True}))
    logins = {"count": 0}

    def fake_login(**_kwargs) -> WebSession:  # noqa: ANN003
        logins["count"] += 1
        return refreshed

    monkeypatch.setattr(
        ws,
        "pooled_requests_session",
        lambda current, _url: fresh_http if current.created_at == 2 else expired_http,
    )
    monkeypatch.setattr(
        ws,
        "_get_browser_client",
        lambda _session: pytest.fail("an expiry must not be retried through a browser"),
    )
    monkeypatch.setattr(ws_auth, "refresh_session", lambda _session, **kwargs: fake_login())
    monkeypatch.setattr(ws, "_close_browser_client", lambda: None)
    monkeypatch.setattr(transport, "_force_browser", False)

    result = transport.request("GET", "")

    assert result == {"ok": True}
    assert logins["count"] == 1
    assert len(expired_http.calls) == 1
    assert len(fresh_http.calls) == 1
    assert transport._force_browser is False


def test_request_json_non_json_triggers_fallback(transport, monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=0,
    )
    transport.adopt_session(session)

    http = DummyHTTP(DummyResponse(200, payload=ValueError("bad json")))
    browser = DummyBrowserClient({"ok": True})

    monkeypatch.setattr(ws, "pooled_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws, "_get_browser_client", lambda _session: browser)
    monkeypatch.setattr(transport, "_force_browser", False)

    result = transport.request("GET", "")

    assert result == {"ok": True}
    assert transport._force_browser is True
    assert http.calls
    assert browser.calls


def test_request_json_transport_error_triggers_fallback(transport, monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=0,
    )
    transport.adopt_session(session)

    class FailingHTTP:
        def __init__(self) -> None:
            self.calls = []

        def get(  # noqa: ANN001
            self, url, headers=None, timeout=None, allow_redirects=True
        ):
            assert allow_redirects is False
            self.calls.append(("GET", url, headers, timeout))
            raise requests.exceptions.SSLError("ssl eof")

        def close(self) -> None:
            pass

    http = FailingHTTP()
    browser = DummyBrowserClient({"ok": True})

    monkeypatch.setattr(ws, "pooled_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws, "_get_browser_client", lambda _session: browser)
    monkeypatch.setattr(transport, "_force_browser", False)

    result = transport.request("GET", "")

    assert result == {"ok": True}
    assert transport._force_browser is True
    assert http.calls
    assert browser.calls


def test_request_json_supports_delete(transport, monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=0,
    )
    transport.adopt_session(session)

    http = DummyHTTP(DummyResponse(200, payload={"ok": True}))

    monkeypatch.setattr(ws, "pooled_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(transport, "_force_browser", False)

    result = transport.request("DELETE", "/api/v2/image?Action=DeleteImage")

    assert result == {"ok": True}
    assert http.calls == [("DELETE", "https://example.test/api/v2/image?Action=DeleteImage", {}, 30)]


def test_request_json_browser_runtime_error_uses_standard_hint(
    transport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=0,
    )
    transport.adopt_session(session)

    def raise_browser_runtime_error(_session):  # noqa: ANN001
        raise RuntimeError(
            "BrowserType.launch: Executable doesn't exist at /tmp/chromium\n"
            "Please run the following command to download new browsers:\n"
            "    playwright install"
        )

    monkeypatch.setattr(ws, "_get_browser_client", raise_browser_runtime_error)
    monkeypatch.setattr(transport, "_force_browser", True)

    with pytest.raises(RuntimeError) as excinfo:
        transport.request("GET", "")

    message = str(excinfo.value)
    assert "inspire update --cli-only" in message
    assert "playwright install" not in message


def test_browser_client_reset_on_expired(transport, monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=0,
    )
    transport.adopt_session(session)

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
    monkeypatch.setattr(transport, "_force_browser", True)
    monkeypatch.setattr(ws_auth, "refresh_session", lambda _session, **kwargs: fake_get_web_session())

    with pytest.raises(ws.SessionExpiredError):
        transport.request("GET", "")

    assert closed["called"] is True


def test_request_json_reauth_is_silent(
    transport,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=0,
    )
    transport.adopt_session(session)
    refreshed = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=1,
    )

    class ExpiringBrowserClient:
        def request_json(self, *_args, **_kwargs):
            raise ws.SessionExpiredError("expired")

    monkeypatch.setattr(ws, "_get_browser_client", lambda _session: ExpiringBrowserClient())
    monkeypatch.setattr(ws, "_close_browser_client", lambda: None)
    monkeypatch.setattr(transport, "_force_browser", True)
    monkeypatch.setattr(ws_auth, "refresh_session", lambda _session, **kwargs: refreshed)
    monkeypatch.setattr(
        ws,
        "pooled_requests_session",
        lambda _session, _url: DummyHTTP(DummyResponse(401)),
    )

    with pytest.raises(ws.SessionExpiredError):
        transport.request("GET", "")

    captured = capsys.readouterr()
    assert "Session expired, re-authenticating..." not in captured.err


def test_request_json_reauth_refreshes_session_in_place(transport, monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-old",
        login_username="old-user",
        created_at=1.0,
    )
    transport.adopt_session(session)
    refreshed = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-new",
        login_username="new-user",
        all_workspace_fair_scheduling={"ws-new": True},
        created_at=2.0,
    )

    class ExpiringBrowserClient:
        def request_json(self, *_args, **_kwargs):
            raise ws.SessionExpiredError("expired")

    expiring = ExpiringBrowserClient()
    http = DummyHTTP(DummyResponse(200, payload={"ok": True}))
    refresh_calls = {"count": 0}

    def fake_get_web_session(**_kwargs):
        refresh_calls["count"] += 1
        return refreshed

    monkeypatch.setattr(ws, "_get_browser_client", lambda _session: expiring)
    monkeypatch.setattr(ws, "pooled_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws, "_close_browser_client", lambda: None)
    monkeypatch.setattr(ws_auth, "refresh_session", lambda _session, **kwargs: fake_get_web_session())
    monkeypatch.setattr(transport, "_force_browser", True)

    result = transport.request("GET", "")
    assert result == {"ok": True}
    assert refresh_calls["count"] == 1
    assert session.access_token == refreshed.access_token
    assert session.workspace_id == refreshed.workspace_id
    assert session.login_username == refreshed.login_username
    assert session.all_workspace_fair_scheduling == refreshed.all_workspace_fair_scheduling
    assert session.created_at == refreshed.created_at
    assert len(http.calls) == 1

    second_result = transport.request("GET", "")
    assert second_result == {"ok": True}
    assert refresh_calls["count"] == 1
    assert len(http.calls) == 2


def test_a_rate_limited_call_still_gets_only_one_session_rebuild(
    transport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transient retry runs the call again; it must not re-arm the login.

    A workspace-wide fan-out walks into the rate limiter routinely, and every
    429 used to hand the next attempt a fresh authentication allowance. Three
    attempts, three allowances, three logins from one expired session.
    """
    from inspire.platform.web.session import retry as ws_retry

    monkeypatch.setattr(ws_retry.time, "sleep", lambda _seconds: None)

    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=1.0,
    )
    transport.adopt_session(session)
    class _RateLimited(DummyResponse):
        headers = {"Retry-After": "0"}

    responses = [_RateLimited(429), DummyResponse(401), DummyResponse(401)]
    logins = {"count": 0}

    class _SequencedHTTP:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, url, headers=None, timeout=None, allow_redirects=True):  # noqa: ANN001
            assert allow_redirects is False
            self.calls += 1
            return responses[min(self.calls - 1, len(responses) - 1)]

    http = _SequencedHTTP()

    def fake_login(**_kwargs) -> WebSession:  # noqa: ANN003
        logins["count"] += 1
        return WebSession(
            access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

            workspace_id="ws-test",
            created_at=2.0 + logins["count"],
        )

    monkeypatch.setattr(ws, "pooled_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws_auth, "refresh_session", lambda _session, **kwargs: fake_login())
    monkeypatch.setattr(ws, "_close_browser_client", lambda: None)
    monkeypatch.setattr(
        ws,
        "_get_browser_client",
        lambda _session: pytest.fail("an expiry must not be retried through a browser"),
    )
    monkeypatch.setattr(transport, "_force_browser", False)

    with pytest.raises(ws.SessionExpiredError):
        transport.request("GET", "")

    assert logins["count"] == 1


def test_a_session_another_caller_already_rebuilt_is_not_rebuilt_again(
    transport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two callers share one session object; the second one's 401 is stale news.

    The 401 belongs to the generation that was sent, and that generation has
    already been replaced in place. Comparing against the session as it reads
    *now* made the replacement look like the thing that failed, and bought a
    second login for a session nobody had tried yet.
    """
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=1.0,
    )
    transport.adopt_session(session)

    class _RefreshedUnderneathHTTP:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, url, headers=None, timeout=None, allow_redirects=True):  # noqa: ANN001
            assert allow_redirects is False
            self.calls += 1
            if self.calls == 1:
                # The other caller finished its refresh while this request was
                # in flight, and wrote the result into the shared object.
                session.access_token = "fresh"
                session.cookies = {"session": "fresh"}
                session.created_at = 2.0
                return DummyResponse(401)
            return DummyResponse(200, payload={"ok": True})

    http = _RefreshedUnderneathHTTP()
    monkeypatch.setattr(ws, "pooled_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(
        ws,
        "_get_web_session",
        lambda **_kwargs: pytest.fail("a session someone else just rebuilt must not log in"),
    )
    monkeypatch.setattr(ws, "_close_browser_client", lambda: None)
    monkeypatch.setattr(transport, "_force_browser", False)

    assert transport.request("GET", "") == {"ok": True}
    assert http.calls == 2


def test_a_login_nothing_can_use_is_not_repeated_for_every_call(
    transport,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The last hole the per-call budget leaves open.

    The budget bounds rebuilds inside one `request_json`, and the credential
    guard bounds *failed* logins. Neither covers a login that keeps succeeding
    and keeps minting a session the platform refuses: every call in a fan-out
    gets a fresh budget and the guard sees nothing wrong. Measured at 5 logins
    for 5 calls before this.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        created_at=1.0,
    )
    transport.adopt_session(session)
    logins = {"count": 0}

    def fake_login(**_kwargs) -> WebSession:  # noqa: ANN003
        logins["count"] += 1
        return WebSession(
            access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

            created_at=1.0 + logins["count"],
        )

    monkeypatch.setattr(
        ws, "pooled_requests_session", lambda *_args: DummyHTTP(DummyResponse(401))
    )
    monkeypatch.setattr(ws_auth, "refresh_session", lambda _session, **kwargs: fake_login())
    monkeypatch.setattr(ws, "_close_browser_client", lambda: None)
    monkeypatch.setattr(
        ws, "_get_browser_client", lambda _session: pytest.fail("no browser expected")
    )
    monkeypatch.setattr(transport, "_force_browser", False)

    for _ in range(5):
        with pytest.raises(ws.SessionExpiredError):
            transport.request("GET", "")

    assert logins["count"] == 1


def test_a_stragglers_success_on_the_old_generation_does_not_vouch_for_the_new(
    transport,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The generation that answered is the one the call actually sent.

    A request sent with the old cookies can return 200 after another thread
    has already minted an unproven replacement in place. Crediting
    `session.created_at` as it reads on return credits that replacement --
    clearing the marker for a login nothing has used, which re-arms exactly
    the repeated logins the marker exists to stop.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        created_at=1.0,
    )
    transport.adopt_session(session)

    class _RebuildLandsMidFlight:
        def get(self, url, headers=None, timeout=None, allow_redirects=True):  # noqa: ANN001
            if session.created_at == 1.0:
                # Another caller's rebuild lands while this request is on the
                # wire; the generation it minted has answered nothing yet.
                session.access_token = "minted"
                session.cookies = {"session": "minted"}
                session.created_at = 2.0
                transport._unproven_rebuild = 2.0
                return DummyResponse(200, payload={"ok": True})
            return DummyResponse(401)

    monkeypatch.setattr(ws, "pooled_requests_session", lambda *_args: _RebuildLandsMidFlight())
    monkeypatch.setattr(ws, "_close_browser_client", lambda: None)
    monkeypatch.setattr(transport, "_force_browser", False)
    monkeypatch.setattr(
        ws,
        "_get_web_session",
        lambda **_kwargs: pytest.fail(
            "a login nothing has been able to use must not be replaced by another"
        ),
    )

    # The straggler's answer belongs to generation 1.0 and must say so.
    assert transport.request("GET", "") == {"ok": True}

    # Generation 2.0 is refused by the platform: no further login, by design.
    with pytest.raises(ws.SessionExpiredError, match="refused as well"):
        transport.request("GET", "")


def test_a_rebuilt_session_that_works_does_not_block_a_later_expiry(
    transport,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The refusal is about an *unusable* login, not about having logged in."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        created_at=1.0,
    )
    transport.adopt_session(session)
    logins = {"count": 0}
    answers: list[DummyResponse] = [DummyResponse(401), DummyResponse(200, payload={"ok": True})]

    class _Sequenced:
        def get(self, url, headers=None, timeout=None, allow_redirects=True):  # noqa: ANN001
            return answers.pop(0) if answers else DummyResponse(200, payload={"ok": True})

    def fake_login(**_kwargs) -> WebSession:  # noqa: ANN003
        logins["count"] += 1
        return WebSession(
            access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

            created_at=1.0 + logins["count"],
        )

    monkeypatch.setattr(ws, "pooled_requests_session", lambda *_args: _Sequenced())
    monkeypatch.setattr(ws_auth, "refresh_session", lambda _session, **kwargs: fake_login())
    monkeypatch.setattr(ws, "_close_browser_client", lambda: None)
    monkeypatch.setattr(transport, "_force_browser", False)

    assert transport.request("GET", "") == {"ok": True}

    # A later expiry on a session that has been answering is a real expiry.
    answers.extend([DummyResponse(401), DummyResponse(200, payload={"ok": True})])
    assert transport.request("GET", "") == {"ok": True}
    assert logins["count"] == 2


def test_browser_request_context_posts_json_bytes():
    client = ws._BrowserRequestClient.__new__(ws._BrowserRequestClient)
    context = DummyBrowserContext()
    client._context = context
    client._closed = False
    client.session = WebSession(access_token="test", refresh_token="refresh", access_expires_at=9999999999, refresh_expires_at=9999999999, created_at=1)
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
    client.session = WebSession(access_token="test", refresh_token="refresh", access_expires_at=9999999999, refresh_expires_at=9999999999, created_at=1)
    client.session_fingerprint = "test"

    result = client.request_json("DELETE", "https://example.test/api/v2/image?Action=DeleteImage")

    assert result == {"ok": True}
    assert context.request.calls
    method, _url, _headers, _data, _timeout = context.request.calls[0]
    assert method == "DELETE"


def test_browser_client_cache_is_thread_local(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

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
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

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
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

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






def test_get_web_session_reads_cached_session_without_explicit_account(
    monkeypatch: pytest.MonkeyPatch,
):
    """Resolve the session cache through the active InspireSkill account."""
    cached = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        login_username="project-user",
        created_at=0,
    )
    load_calls: list[str | None] = []

    def fake_load(cls, allow_expired=False, account=None):  # type: ignore[no-untyped-def]
        load_calls.append(account)
        return cached

    monkeypatch.setattr(ws_auth.WebSession, "load", classmethod(fake_load))

    session = ws_auth.get_web_session(force_refresh=False, require_workspace=False)

    assert session is cached
    assert load_calls
    # Caller passes no explicit account; internal resolution handles it.
    assert load_calls[0] is None






def test_asyncio_browser_fallback_uses_disposable_clients(transport, monkeypatch: pytest.MonkeyPatch):
    """Two consecutive browser-backed requests from an asyncio context must each
    get their own disposable _BrowserRequestClient — not the global cached one —
    to avoid cross-thread greenlet / thread-affinity errors.
    """
    import asyncio
    import threading

    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        workspace_id="ws-test",
        created_at=0,
    )
    transport.adopt_session(session)

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
    monkeypatch.setattr(transport, "_force_browser", True)
    monkeypatch.setattr(ws, "_get_browser_client", _fail_global_cache)

    async def two_requests():
        r1 = transport.request("GET", "/1")
        r2 = transport.request("GET", "/2")
        return r1, r2

    r1, r2 = asyncio.run(two_requests())

    assert r1 == {"ok": True}
    assert r2 == {"ok": True}
    assert len(created) == 2, "each call should create its own client"
    assert created[0] is not created[1], "clients must not be reused"
    assert all(c.closed for c in created), "disposable clients must be closed"


def test_clear_session_cache_removes_only_active_account_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_account_session_storage,  # noqa: ANN001, ARG001
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    accounts_root = fake_home / ".inspire" / "accounts"
    (accounts_root / "alice").mkdir(parents=True)
    (accounts_root / "alice" / "web_session.json").write_text("{}", encoding="utf-8")
    (accounts_root / "bob").mkdir()
    (accounts_root / "bob" / "web_session.json").write_text("{}", encoding="utf-8")
    (accounts_root / "bob" / "config.toml").write_text("", encoding="utf-8")  # unrelated file kept
    (fake_home / ".inspire" / "current").write_text("alice\n", encoding="utf-8")

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
    (accounts_root / "alice" / "web_session.json").write_text("{}", encoding="utf-8")
    (accounts_root / "bob").mkdir()
    (accounts_root / "bob" / "web_session.json").write_text("{}", encoding="utf-8")
    (accounts_root / "bob" / "config.toml").write_text("", encoding="utf-8")  # unrelated file kept

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


def test_save_writes_to_account_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_account_session_storage,  # noqa: ANN001, ARG001
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    import inspire.accounts as accounts_mod

    monkeypatch.setattr(accounts_mod, "current_account", lambda: "alice")

    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,

        login_username="platform-user",
        created_at=time.time(),
    )
    session.save()

    target = fake_home / ".inspire" / "accounts" / "alice" / "web_session.json"
    assert target.exists()


def test_save_prefers_bound_account_over_current_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    import inspire.accounts as accounts_mod

    monkeypatch.setattr(accounts_mod, "current_account", lambda: "beta")

    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,
        created_at=time.time(),
        login_username="alpha-user",
        account="alpha",
    )
    session.save()

    alpha_cache = (
        fake_home / ".inspire" / "accounts" / "alpha" / "web_session.json"
    )
    beta_cache = fake_home / ".inspire" / "accounts" / "beta" / "web_session.json"
    assert json.loads(alpha_cache.read_text(encoding="utf-8"))["account"] == "alpha"
    assert not beta_cache.exists()


def test_loaded_session_stays_bound_after_current_account_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_account_session_storage,  # noqa: ANN001, ARG001
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    import inspire.accounts as accounts_mod

    active_account = {"name": "alpha"}
    monkeypatch.setattr(
        accounts_mod,
        "current_account",
        lambda: active_account["name"],
    )

    alpha_cache = (
        fake_home / ".inspire" / "accounts" / "alpha" / "web_session.json"
    )
    alpha_cache.parent.mkdir(parents=True)
    original = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,
        created_at=time.time(),
        login_username="before",
        account="alpha",
    )
    alpha_cache.write_text(json.dumps(original.to_dict()), encoding="utf-8")

    loaded = WebSession.load()
    assert loaded is not None
    assert loaded.account == "alpha"

    active_account["name"] = "beta"
    loaded.login_username = "after"
    loaded.save()

    beta_cache = fake_home / ".inspire" / "accounts" / "beta" / "web_session.json"
    assert json.loads(alpha_cache.read_text(encoding="utf-8"))["login_username"] == "after"
    assert not beta_cache.exists()


def test_save_explicit_account_overrides_bound_and_current_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    import inspire.accounts as accounts_mod

    monkeypatch.setattr(accounts_mod, "current_account", lambda: "beta")

    session = WebSession(
        access_token="test-access", refresh_token="test-refresh", access_expires_at=9999999999, refresh_expires_at=9999999999,
        created_at=time.time(),
        account="alpha",
    )
    session.save(account="gamma")

    accounts_root = fake_home / ".inspire" / "accounts"
    gamma_cache = accounts_root / "gamma" / "web_session.json"
    assert json.loads(gamma_cache.read_text(encoding="utf-8"))["account"] == "gamma"
    assert session.account == "gamma"
    assert not (accounts_root / "alpha" / "web_session.json").exists()
    assert not (accounts_root / "beta" / "web_session.json").exists()


def test_credentials_do_not_influence_account_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    import inspire.accounts as accounts_mod

    monkeypatch.setattr(accounts_mod, "current_account", lambda: None)
    monkeypatch.setenv("INSPIRE_USERNAME", "ghost")

    from inspire.platform.web.session.models import get_session_cache_file

    path = get_session_cache_file()
    # Must NOT resolve to anything under accounts/ghost/...
    assert "accounts/ghost" not in str(path)


@pytest.fixture
def transport():
    from inspire.platform.web.transport import Transport

    transport = Transport(None, "https://example.test", username="", allow_browser=True,
                          cli_compat=True)
    yield transport
    transport.close()
