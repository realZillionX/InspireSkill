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
from inspire.platform.web.session import proxy as ws_proxy
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
                            "is_fair_workspace": True,
                        },
                    ],
                },
            ]
        }
    }

    ids, names, fair_scheduling = ws_auth._workspace_routes_from_payload(payload)

    assert ids == [
        "ws-11111111-1111-1111-1111-111111111111",
        "ws-22222222-2222-2222-2222-222222222222",
    ]
    assert names == {
        "ws-11111111-1111-1111-1111-111111111111": "CPU资源空间",
        "ws-22222222-2222-2222-2222-222222222222": "分布式训练空间",
    }
    assert fair_scheduling == {
        "ws-11111111-1111-1111-1111-111111111111": False,
        "ws-22222222-2222-2222-2222-222222222222": True,
    }


def test_workspace_routes_from_payload_reads_the_v2_envelope() -> None:
    """Login discovery is `user.GetRoutes`, so the routes arrive under `Result`."""
    payload = {
        "ResponseMetadata": {"Action": "GetRoutes"},
        "Result": {
            "routes": [
                {
                    "name": "userWorkspaceList",
                    "routes": [
                        {
                            "name": "CPU资源空间",
                            "path": "ws-11111111-1111-1111-1111-111111111111",
                            "is_fair_workspace": True,
                        }
                    ],
                }
            ]
        },
    }

    ids, names, fair_scheduling = ws_auth._workspace_routes_from_payload(payload)

    assert ids == ["ws-11111111-1111-1111-1111-111111111111"]
    assert names == {"ws-11111111-1111-1111-1111-111111111111": "CPU资源空间"}
    assert fair_scheduling == {"ws-11111111-1111-1111-1111-111111111111": True}


def test_workspace_routes_from_payload_raises_on_a_v2_error_envelope() -> None:
    """A refused request must not read as "this account has no workspaces"."""
    payload = {
        "ResponseMetadata": {
            "Error": {"Code": "InvalidParameter", "Message": "WorkspaceId is required"}
        }
    }

    with pytest.raises(ValueError, match="WorkspaceId is required"):
        ws_auth._workspace_routes_from_payload(payload)


def test_bootstrap_calls_are_v2_actions() -> None:
    """The two requests that run before a session exists have no v1 form left."""
    assert ws_auth.USER_DETAIL_PATH == "/api/v2/user?Action=GetUserDetail"
    assert ws_auth.USER_ROUTES_PATH == "/api/v2/user?Action=GetRoutes"
    # The literal `default` is the placeholder for "no workspace known yet"; the
    # gateway accepts it and answers the same rows a real id does. An empty
    # string or a missing key is rejected with `WorkspaceId is required`.
    assert ws_auth.BOOTSTRAP_ROUTES_BODY == {"WorkspaceId": "default"}


def test_web_session_round_trip_preserves_workspace_capabilities() -> None:
    session = WebSession(
        storage_state={
            "cookies": [{"name": "session", "value": "secret"}],
            "origins": [],
        },
        workspace_id="ws-test",
        all_workspace_fair_scheduling={"ws-test": True, "ws-standard": False},
        created_at=1.0,
    )

    restored = WebSession.from_dict(session.to_dict())

    assert restored.all_workspace_fair_scheduling == {
        "ws-test": True,
        "ws-standard": False,
    }
    assert restored.cookies == {"session": "secret"}


def test_cas_page_encrypt_password_matches_page_rsa() -> None:
    assert (
        ws_auth._cas_page_encrypt_password("abc", CAS_RSA_EXPONENT, CAS_RSA_MODULUS)
        == "050d4541820093722eb891339242b9e3147ba98618ed03dd97dc98f4719b0a76"
        "a139138a7087ca84ee933dc56d7e7fa615a2dbcd4cda0f356eabedd98616a7a5"
        "cb06926a5005f4c1fe367725e3c0d4651889c92eec7912eb6b01e8edc342acb5"
        "bb11bd05b8bbd51cb4111954df11bcaf2b904c6eabddf6e1a881d57d95490cd5"
    )
    assert (
        ws_auth._cas_page_encrypt_password("password123", CAS_RSA_EXPONENT, CAS_RSA_MODULUS)
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


def test_extract_login_failure_hint_ignores_benign_login_page_copy() -> None:
    html = """
    <html><body>
      <li class="auth_ul_li2"><h3>验证码登录</h3></li>
      <input class="auth_input paw_input" type="text" placeholder="验证码">
      <img src="https://example.test/validateimage">
      <!-- 验证码登录 -->
      <span class="sendMsg">发送验证码</span>
      <input type="text" class="auth_input" placeholder="动态验证码">
      <div class="form-error"><span class="form-tab-nav"></div>
      <div class="form-error"><span class="form-tab-nav"></span></div>
      <div class="form-error-message">需要输入验证码</div>
    </body></html>
    """

    assert ws_auth._extract_login_failure_hint(html) == ""


@pytest.mark.parametrize(
    "message",
    [
        "账号或密码错误，请重新输入",
        "验证码信息无效。",
        "账号已被锁定，请 30 分钟后重试",
    ],
)
def test_extract_login_failure_hint_preserves_explicit_error(message: str) -> None:
    html = f'<div class="form-error">{message}</div>'

    assert ws_auth._extract_login_failure_hint(html) == message


def test_extract_login_failure_hint_handles_nested_markup() -> None:
    html = """
    <div class="form-error"><span class="form-tab-nav"></div>
    <div class='field form-error visible'>
      <script>var password = "secret";</script>
      <span>登录过于频繁</span><br>请稍后 &amp; 再试
    </div>
    """

    assert ws_auth._extract_login_failure_hint(html) == "登录过于频繁 请稍后 & 再试"


def test_extract_login_failure_hint_ignores_hidden_and_inert_descendants() -> None:
    html = """
    <div class="form-error">
      账号或密码错误
      <span hidden>验证码错误</span>
      <span inert>用户名错误</span>
      <span aria-hidden="true">密码错误</span>
      <span class="d-none">账号错误</span>
    </div>
    """

    assert ws_auth._extract_login_failure_hint(html) == "账号或密码错误"


@pytest.mark.parametrize(
    "opening_tag",
    [
        '<div class="form-error" hidden>',
        '<div class="form-error" inert>',
        '<div class="form-error" aria-hidden="true">',
        '<div class="form-error" style="display: none">',
        '<div class="form-error" style="visibility: hidden">',
        '<div class="form-error" style="opacity: 0">',
        '<div class="form-error hidden">',
        '<div class="form-error Hidden">',
    ],
)
def test_extract_login_failure_hint_ignores_hidden_error_containers(opening_tag: str) -> None:
    html = (
        f'{opening_tag}验证码信息无效</div><div class="form-error">账号已被锁定，请稍后重试</div>'
    )

    assert ws_auth._extract_login_failure_hint(html) == "账号已被锁定，请稍后重试"


@pytest.mark.parametrize(
    "opening_tag",
    [
        "<section hidden>",
        "<section inert>",
        '<section aria-hidden="true">',
        '<section style="display: none">',
    ],
)
def test_extract_login_failure_hint_ignores_hidden_ancestors(opening_tag: str) -> None:
    html = (
        f'{opening_tag}<section><div class="form-error">验证码信息无效</div></section></section>'
        '<div class="form-error">账号已被锁定，请稍后重试</div>'
    )

    assert ws_auth._extract_login_failure_hint(html) == "账号已被锁定，请稍后重试"


def test_extract_login_failure_hint_ignores_inert_markup() -> None:
    html = """
    <template><div class="form-error">验证码信息无效</div></template>
    <noscript><div class="form-error">用户名或密码错误</div></noscript>
    <div class="form-error">登录过于频繁，请稍后再试</div>
    """

    assert ws_auth._extract_login_failure_hint(html) == "登录过于频繁，请稍后再试"


def test_extract_login_failure_hint_handles_truncated_error_container() -> None:
    html = '<div class="form-error"><span>账号已被锁定'

    assert ws_auth._extract_login_failure_hint(html) == "账号已被锁定"
    assert ws_auth._extract_login_failure_hint('<input class="form-error">验证码登录') == ""


def test_extract_login_failure_hint_applies_length_limit() -> None:
    html = '<div class="form-error">账号已被锁定，请稍后重试</div>'

    assert ws_auth._extract_login_failure_hint(html, limit=6) == "账号已被锁定"
    assert ws_auth._extract_login_failure_hint(html, limit=0) == ""


def test_extract_page_login_failure_hint_uses_structured_html() -> None:
    class Page:
        def __init__(self) -> None:
            self.content_calls = 0

        def content(self) -> str:
            self.content_calls += 1
            return '<div class="form-error">账号或密码错误</div>'

    page = Page()

    assert ws_auth._extract_page_login_failure_hint(page) == "账号或密码错误"
    assert page.content_calls == 1


def test_extract_page_login_failure_hint_tolerates_closed_page() -> None:
    class ClosedPage:
        def content(self) -> str:
            raise RuntimeError("page closed")

    assert ws_auth._extract_page_login_failure_hint(ClosedPage()) == ""


def test_login_not_complete_message_prioritizes_platform_error() -> None:
    message = ws_auth._login_not_complete_message(
        status=401,
        current_url="https://cas.sii.edu.cn/login",
        page_hint="账号或密码错误",
        proxy_source="requests:system_env",
        base_proxy_route="proxy",
    )

    assert "Login did not complete." in message
    assert "Platform reported: 账号或密码错误" in message
    assert "platform login name" not in message
    assert "CAPTCHA" not in message
    assert "inspire account check --details" in message
    assert "last auth check status=401" in message
    assert "Shell HTTP_PROXY/HTTPS_PROXY/ALL_PROXY is configured" in message
    assert "CAS/Keycloak redirects may match NO_PROXY differently" in message
    assert "proxy_source=requests:system_env, base_route=proxy" in message


def test_login_not_complete_message_uses_general_advice_without_platform_error() -> None:
    message = ws_auth._login_not_complete_message(status=401)

    assert "Platform reported:" not in message
    assert "platform login name" in message
    assert "*.sii.edu.cn" in message
    assert "CAPTCHA" in message


def test_explicit_cas_failure_is_not_hidden_by_playwright_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_requests_login(*_args, **_kwargs):  # noqa: ANN001
        raise ws_auth._CasLoginFailure("Platform reported: 账号或密码错误")

    monkeypatch.setattr(ws_auth, "_login_with_cas_requests", fail_requests_login)

    with pytest.raises(ws_auth._CasLoginFailure, match="账号或密码错误"):
        ws_auth.login_with_playwright(
            "user",
            "password",
            base_url="https://qz.sii.edu.cn",
        )


class _FakePlaywrightLogin:
    """The browser path with no browser: a page that never authenticates.

    *form_found* is the only knob that matters. With it, the password was typed
    and submitted before the wait timed out; without it, the form was never
    there and nothing left the machine — and those two have to end differently.
    """

    def __init__(self, *, form_found: bool, page_html: str = "") -> None:
        self.form_found = form_found
        self.page_html = page_html

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        outer = self

        class Locator:
            def __init__(self) -> None:
                self.first = self

            def fill(self, _value: str) -> None:
                pass

            def press(self, _key: str, timeout: int) -> None:
                pass

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

        class Context:
            request = object()

            def new_page(self) -> Page:
                return Page()

        class Browser:
            def new_context(self, **_kwargs) -> Context:
                return Context()

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


def test_playwright_failure_after_submission_is_an_authentication_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The browser is the last transport, so its failures must still be typed.

    Nothing falls back after it, but the circuit still has to open: the next
    process to reach an expired session would otherwise submit the same
    rejected password again.
    """
    from inspire.platform.web.session import login_guard

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".inspire" / "accounts" / "alice").mkdir(parents=True)
    _FakePlaywrightLogin(
        form_found=True,
        page_html='<div class="form-error">账号已被锁定</div>',
    ).install(monkeypatch)

    with pytest.raises(ws.AuthenticationError, match="账号已被锁定") as excinfo:
        ws_auth.login_with_playwright(
            "alice",
            "password",
            base_url="https://qz.sii.edu.cn",
            account="alice",
        )

    assert "The password reached CAS" in str(excinfo.value)
    assert login_guard.block_file("alice").exists()


def test_playwright_failure_before_submission_is_not_an_authentication_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No form, no submission — so no rejected credential to remember."""
    from inspire.platform.web.session import login_guard

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".inspire" / "accounts" / "alice").mkdir(parents=True)
    _FakePlaywrightLogin(form_found=False).install(monkeypatch)

    with pytest.raises(ValueError) as excinfo:
        ws_auth.login_with_playwright(
            "alice",
            "password",
            base_url="https://qz.sii.edu.cn",
            account="alice",
        )

    assert not isinstance(excinfo.value, ws.AuthenticationError)
    assert "The password reached CAS" not in str(excinfo.value)
    assert not login_guard.block_file("alice").exists()


def test_cas_server_failure_after_submission_does_not_submit_again_in_a_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5xx after the password POST is not a reason to try the other channel.

    The request went out. Whether CAS counted it is not observable from here,
    so the browser fallback -- which would submit it a second time -- is closed
    for every outcome past that line, not only for explicit rejections.
    """
    login_html = f"""
    <form action="/cas/login">
      <input name="username" value="">
      <input name="password" value="">
      <input name="execution" value="exec-1">
    </form>
    <script>
      RSAUtils.getKeyPair("{CAS_RSA_EXPONENT}", "", "{CAS_RSA_MODULUS}");
    </script>
    """

    class Response:
        def __init__(self, status_code: int, text: str, url: str) -> None:
            self.status_code = status_code
            self.text = text
            self.url = url

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))

    class HTTP(requests.Session):
        def __init__(self) -> None:
            super().__init__()
            self.submissions = 0

        def get(self, url, **_kwargs):  # noqa: ANN001
            return Response(200, login_html, "https://cas.sii.edu.cn/cas/login")

        def post(self, url, **_kwargs):  # noqa: ANN001
            self.submissions += 1
            return Response(503, "temporarily unavailable", url)

    http = HTTP()
    monkeypatch.setattr(requests, "Session", lambda: http)
    monkeypatch.setattr(
        ws_proxy,
        "resolve_requests_proxy_config",
        lambda account=None: ({}, "none"),
    )
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: pytest.fail("credentials must not be submitted again through Playwright"),
    )

    with pytest.raises(ws.AuthenticationError, match="The password reached CAS"):
        ws_auth.login_with_playwright("user", "password", base_url="https://qz.sii.edu.cn")

    assert http.submissions == 1


def test_a_login_page_asking_for_a_verification_code_submits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAS wants a human. Submitting the password anyway just loses an attempt.

    Measured against the live platform: after repeated failed logins from one
    machine, CAS adds `authcode` to the password form and answers every
    submission with "account or password is wrong" — so the CLI reported a
    credential problem for an account whose credentials were fine, and each
    retry spent a real login attempt on a form it could not complete.
    """
    login_html = f"""
    <form id="fm1" action="/cas/login">
      <input name="username" value="">
      <input name="password" value="">
      <input name="execution" value="exec-1">
      <input id="authcode" name="authcode" value="" placeholder="验证码 Verification Code">
    </form>
    <script>RSAUtils.getKeyPair("{CAS_RSA_EXPONENT}", "", "{CAS_RSA_MODULUS}");</script>
    """

    class Response:
        status_code = 200
        text = login_html
        url = "https://cas.sii.edu.cn/cas/login"

        def raise_for_status(self) -> None:
            return None

    class HTTP(requests.Session):
        def __init__(self) -> None:
            super().__init__()
            self.posts = 0

        def get(self, url, **_kwargs):  # noqa: ANN001
            return Response()

        def post(self, url, **_kwargs):  # noqa: ANN001
            self.posts += 1
            raise AssertionError("nothing may be submitted to a form asking for a code")

    http = HTTP()
    monkeypatch.setattr(requests, "Session", lambda: http)
    monkeypatch.setattr(
        ws_proxy,
        "resolve_requests_proxy_config",
        lambda account=None: ({}, "none"),
    )
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: pytest.fail("the browser cannot answer a verification code either"),
    )

    with pytest.raises(ValueError, match="verification code") as excinfo:
        ws_auth.login_with_playwright("user", "password", base_url="https://qz.sii.edu.cn")

    assert not isinstance(excinfo.value, ws.AuthenticationError)
    assert "No credentials were submitted" in str(excinfo.value)
    assert http.posts == 0


def test_a_verification_code_prompt_does_not_open_the_login_circuit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No submission, no recorded failure — the next real attempt is not blocked."""
    from inspire.platform.web.session import login_guard

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".inspire" / "accounts" / "alice").mkdir(parents=True)

    with pytest.raises(ValueError):
        with login_guard.guarded_credential_submission("alice", "secret", account="alice"):
            raise ws_auth._CasVerificationRequired("code required")

    assert not login_guard.block_file("alice").exists()


def test_a_code_field_that_appears_only_in_the_answer_is_still_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAS can add the field in response to the submission it just refused.

    That is the same rejection wearing a different cause, and "check that the
    password is correct" sends the user off to change something that was never
    wrong -- observed against the live platform.
    """
    plain_form = f"""
    <form id="fm1" action="/cas/login">
      <input name="username" value=""><input name="password" value="">
      <input name="execution" value="exec-1">
    </form>
    <script>RSAUtils.getKeyPair("{CAS_RSA_EXPONENT}", "", "{CAS_RSA_MODULUS}");</script>
    """
    form_with_code = plain_form.replace(
        '<input name="execution" value="exec-1">',
        '<input name="execution" value="exec-1"><input name="authcode" value="">',
    )

    class Response:
        def __init__(self, status_code: int, text: str) -> None:
            self.status_code = status_code
            self.text = text
            self.url = "https://cas.sii.edu.cn/cas/login"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {}

    class HTTP(requests.Session):
        def get(self, url, **_kwargs):  # noqa: ANN001
            return Response(200, plain_form)

        def post(self, url, **_kwargs):  # noqa: ANN001
            # The submission is answered with a page that now wants a code.
            if "GetUserDetail" in url:
                return Response(401, form_with_code)
            return Response(200, form_with_code)

    monkeypatch.setattr(requests, "Session", lambda: HTTP())
    monkeypatch.setattr(
        ws_proxy, "resolve_requests_proxy_config", lambda account=None: ({}, "none")
    )
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: pytest.fail("the browser cannot answer a verification code either"),
    )

    with pytest.raises(ws.AuthenticationError) as excinfo:
        ws_auth.login_with_playwright("user", "password", base_url="https://qz.sii.edu.cn")

    message = str(excinfo.value)
    assert "asking for a verification code" in message
    assert "Check that the password is correct" not in message


def test_a_lost_response_after_submission_is_an_authentication_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_html = f"""
    <form action="/cas/login">
      <input name="username" value="">
      <input name="password" value="">
    </form>
    <script>RSAUtils.getKeyPair("{CAS_RSA_EXPONENT}", "", "{CAS_RSA_MODULUS}");</script>
    """

    class Response:
        status_code = 200
        text = login_html
        url = "https://cas.sii.edu.cn/cas/login"

        def raise_for_status(self) -> None:
            return None

    class HTTP(requests.Session):
        def __init__(self) -> None:
            super().__init__()
            self.submissions = 0

        def get(self, url, **_kwargs):  # noqa: ANN001
            return Response()

        def post(self, url, **_kwargs):  # noqa: ANN001
            self.submissions += 1
            raise requests.exceptions.ConnectionError("connection reset")

    http = HTTP()
    monkeypatch.setattr(requests, "Session", lambda: http)
    monkeypatch.setattr(
        ws_proxy,
        "resolve_requests_proxy_config",
        lambda account=None: ({}, "none"),
    )
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: pytest.fail("a lost response must not become a second submission"),
    )

    with pytest.raises(ws.AuthenticationError):
        ws_auth.login_with_playwright("user", "password", base_url="https://qz.sii.edu.cn")

    assert http.submissions == 1


def test_a_cache_write_failure_does_not_discard_an_accepted_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The platform said yes. A local disk problem must not undo that."""
    login_html = f"""
    <form action="/cas/login">
      <input name="username" value="">
      <input name="password" value="">
    </form>
    <script>RSAUtils.getKeyPair("{CAS_RSA_EXPONENT}", "", "{CAS_RSA_MODULUS}");</script>
    """

    class Response:
        def __init__(self, status_code: int, payload: dict | None = None) -> None:
            self.status_code = status_code
            self.text = login_html
            self.url = "https://cas.sii.edu.cn/cas/login"
            self._payload = payload or {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class HTTP(requests.Session):
        def get(self, url, **_kwargs):  # noqa: ANN001
            return Response(200)

        def post(self, url, **_kwargs):  # noqa: ANN001
            return Response(200, {"Result": {"id": "user-one"}})

    monkeypatch.setattr(requests, "Session", lambda: HTTP())
    monkeypatch.setattr(
        ws_proxy,
        "resolve_requests_proxy_config",
        lambda account=None: ({}, "none"),
    )

    def explode(self, account=None):  # noqa: ANN001, ANN202, ARG001
        raise OSError("read-only file system")

    monkeypatch.setattr(WebSession, "save", explode)

    session = ws_auth.login_with_playwright(
        "user",
        "password",
        base_url="https://qz.sii.edu.cn",
    )

    assert session.login_username == "user"


def test_describe_proxy_config_redacts_credentials() -> None:
    assert ws_auth._describe_proxy_config(
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


def test_cas_requests_login_honors_no_proxy_for_system_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_standard_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:18080")
    monkeypatch.setenv("HTTPS_PROXY", "http://secure-proxy.example:18443")
    monkeypatch.setenv("ALL_PROXY", "http://all-proxy.example:18081")
    monkeypatch.setenv("NO_PROXY", ".sii.edu.cn")
    session = _RecordingRequestsSession()
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr(
        ws_proxy,
        "resolve_requests_proxy_config",
        lambda account=None: (
            {
                "http": "http://proxy.example:18080",
                "https": "http://secure-proxy.example:18443",
            },
            "system_env",
        ),
    )

    with pytest.raises(_StopCASProbe):
        ws_auth._login_with_cas_requests(
            "user",
            "password",
            base_url="https://qz.sii.edu.cn",
        )

    assert session.trust_env is True
    assert session.proxies == {}
    assert session.request_url == "https://qz.sii.edu.cn/login"
    assert session.effective_proxies == {}
    external_settings = session.merge_environment_settings(
        "https://example.com", {}, None, None, None
    )
    assert external_settings["proxies"]["https"] == "http://secure-proxy.example:18443"
    assert external_settings["proxies"]["all"] == "http://all-proxy.example:18081"


@pytest.mark.parametrize("source", ["toml", "explicit_env"])
def test_cas_requests_login_forces_explicit_proxy_despite_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    _clear_standard_proxy_env(monkeypatch)
    monkeypatch.setenv("NO_PROXY", ".sii.edu.cn")
    proxies = {
        "http": "http://proxy.example:18080",
        "https": "http://secure-proxy.example:18443",
    }
    session = _RecordingRequestsSession()
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr(
        ws_proxy,
        "resolve_requests_proxy_config",
        lambda account=None: (proxies, source),
    )

    with pytest.raises(_StopCASProbe):
        ws_auth._login_with_cas_requests(
            "user",
            "password",
            base_url="https://qz.sii.edu.cn",
        )

    assert session.trust_env is False
    assert session.proxies == proxies
    assert session.effective_proxies == proxies


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


def test_playwright_install_hint_has_no_dependency_mode_argument() -> None:
    assert browser_launch.playwright_install_hint() == "inspire update --cli-only"


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
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
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


def test_pooled_requests_session_never_answers_with_stale_cookies() -> None:
    # Only the pool is shared. A refreshed session's cookies must replace the
    # previous ones outright, not merge with them.
    first_session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "old"}]},
        cookies={"session": "old"},
        workspace_id="ws-test",
        created_at=0,
    )
    refreshed = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "new"}]},
        cookies={"session": "new"},
        workspace_id="ws-test",
        created_at=1,
    )
    try:
        http = ws_requests_module.pooled_requests_session(first_session, "https://qz.sii.edu.cn")
        http.cookies.set("drive-by", "from-a-response", domain="qz.sii.edu.cn", path="/")

        http = ws_requests_module.pooled_requests_session(refreshed, "https://qz.sii.edu.cn")

        assert http.cookies.get("session", domain="qz.sii.edu.cn") == "new"
        assert http.cookies.get("drive-by", domain="qz.sii.edu.cn") is None
    finally:
        ws_requests_module.close_pooled_requests_session()


@pytest.mark.parametrize("status_code", [401, 302])
def test_request_json_auth_response_rebuilds_once_without_a_browser(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    """401 and 302 both mean "not authenticated", and cost exactly one rebuild.

    Replaying the same refused cookies through Playwright first was pure
    latency, and it is the step that used to leave a browser transport pinned
    on for the rest of the process.
    """
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "expired"}]},
        cookies={"session": "expired"},
        workspace_id="ws-test",
        created_at=1,
    )
    refreshed = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "fresh"}]},
        cookies={"session": "fresh"},
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
    monkeypatch.setattr(ws, "_get_web_session", fake_login)
    monkeypatch.setattr(ws, "_close_browser_client", lambda: None)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", False)

    result = ws.request_json(session, "GET", "https://example.test")

    assert result == {"ok": True}
    assert logins["count"] == 1
    assert len(expired_http.calls) == 1
    assert len(fresh_http.calls) == 1
    assert ws._BROWSER_API_FORCE_BROWSER is False


def test_request_json_non_json_triggers_fallback(monkeypatch: pytest.MonkeyPatch):
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )

    http = DummyHTTP(DummyResponse(200, payload=ValueError("bad json")))
    browser = DummyBrowserClient({"ok": True})

    monkeypatch.setattr(ws, "pooled_requests_session", lambda _session, _url: http)
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

    monkeypatch.setattr(ws, "pooled_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", False)

    result = ws.request_json(session, "DELETE", "https://example.test/api/v2/image?Action=DeleteImage")

    assert result == {"ok": True}
    assert http.calls == [("DELETE", "https://example.test/api/v2/image?Action=DeleteImage", {}, 30)]


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
    monkeypatch.setattr(ws, "_get_web_session", fake_get_web_session)

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
    monkeypatch.setattr(ws, "_get_web_session", lambda **_kwargs: refreshed)
    monkeypatch.setattr(
        ws,
        "pooled_requests_session",
        lambda _session, _url: DummyHTTP(DummyResponse(401)),
    )

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
    monkeypatch.setattr(ws, "_get_web_session", fake_get_web_session)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", True)

    result = ws.request_json(session, "GET", "https://example.test")
    assert result == {"ok": True}
    assert refresh_calls["count"] == 1
    assert session.storage_state == refreshed.storage_state
    assert session.cookies == refreshed.cookies
    assert session.workspace_id == refreshed.workspace_id
    assert session.login_username == refreshed.login_username
    assert session.all_workspace_fair_scheduling == refreshed.all_workspace_fair_scheduling
    assert session.created_at == refreshed.created_at
    assert len(http.calls) == 1

    second_result = ws.request_json(session, "GET", "https://example.test")
    assert second_result == {"ok": True}
    assert refresh_calls["count"] == 1
    assert len(http.calls) == 2


def test_a_rate_limited_call_still_gets_only_one_session_rebuild(
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
        storage_state={"cookies": [{"name": "session", "value": "expired"}]},
        cookies={"session": "expired"},
        workspace_id="ws-test",
        created_at=1.0,
    )
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
            storage_state={"cookies": [{"name": "session", "value": "fresh"}]},
            cookies={"session": "fresh"},
            workspace_id="ws-test",
            created_at=2.0 + logins["count"],
        )

    monkeypatch.setattr(ws, "pooled_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws, "_get_web_session", fake_login)
    monkeypatch.setattr(ws, "_close_browser_client", lambda: None)
    monkeypatch.setattr(
        ws,
        "_get_browser_client",
        lambda _session: pytest.fail("an expiry must not be retried through a browser"),
    )
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", False)

    with pytest.raises(ws.SessionExpiredError):
        ws.request_json(session, "GET", "https://example.test")

    assert logins["count"] == 1


def test_a_session_another_caller_already_rebuilt_is_not_rebuilt_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two callers share one session object; the second one's 401 is stale news.

    The 401 belongs to the generation that was sent, and that generation has
    already been replaced in place. Comparing against the session as it reads
    *now* made the replacement look like the thing that failed, and bought a
    second login for a session nobody had tried yet.
    """
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "expired"}]},
        cookies={"session": "expired"},
        workspace_id="ws-test",
        created_at=1.0,
    )

    class _RefreshedUnderneathHTTP:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, url, headers=None, timeout=None, allow_redirects=True):  # noqa: ANN001
            assert allow_redirects is False
            self.calls += 1
            if self.calls == 1:
                # The other caller finished its refresh while this request was
                # in flight, and wrote the result into the shared object.
                session.storage_state = {"cookies": [{"name": "session", "value": "fresh"}]}
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
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", False)

    assert ws.request_json(session, "GET", "https://example.test") == {"ok": True}
    assert http.calls == 2


def test_a_login_nothing_can_use_is_not_repeated_for_every_call(
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
        storage_state={"cookies": [{"name": "session", "value": "stale"}]},
        cookies={"session": "stale"},
        created_at=1.0,
    )
    logins = {"count": 0}

    def fake_login(**_kwargs) -> WebSession:  # noqa: ANN003
        logins["count"] += 1
        return WebSession(
            storage_state={"cookies": [{"name": "session", "value": "fresh"}]},
            cookies={"session": "fresh"},
            created_at=1.0 + logins["count"],
        )

    monkeypatch.setattr(
        ws, "pooled_requests_session", lambda *_args: DummyHTTP(DummyResponse(401))
    )
    monkeypatch.setattr(ws, "_get_web_session", fake_login)
    monkeypatch.setattr(ws, "_close_browser_client", lambda: None)
    monkeypatch.setattr(
        ws, "_get_browser_client", lambda _session: pytest.fail("no browser expected")
    )
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", False)

    for _ in range(5):
        with pytest.raises(ws.SessionExpiredError):
            ws.request_json(session, "GET", "https://example.test")

    assert logins["count"] == 1


def test_a_rebuilt_session_that_works_does_not_block_a_later_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The refusal is about an *unusable* login, not about having logged in."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    session = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "stale"}]},
        cookies={"session": "stale"},
        created_at=1.0,
    )
    logins = {"count": 0}
    answers: list[DummyResponse] = [DummyResponse(401), DummyResponse(200, payload={"ok": True})]

    class _Sequenced:
        def get(self, url, headers=None, timeout=None, allow_redirects=True):  # noqa: ANN001
            return answers.pop(0) if answers else DummyResponse(200, payload={"ok": True})

    def fake_login(**_kwargs) -> WebSession:  # noqa: ANN003
        logins["count"] += 1
        return WebSession(
            storage_state={"cookies": [{"name": "session", "value": "fresh"}]},
            cookies={"session": "fresh"},
            created_at=1.0 + logins["count"],
        )

    monkeypatch.setattr(ws, "pooled_requests_session", lambda *_args: _Sequenced())
    monkeypatch.setattr(ws, "_get_web_session", fake_login)
    monkeypatch.setattr(ws, "_close_browser_client", lambda: None)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", False)

    assert ws.request_json(session, "GET", "https://example.test") == {"ok": True}

    # A later expiry on a session that has been answering is a real expiry.
    answers.extend([DummyResponse(401), DummyResponse(200, payload={"ok": True})])
    assert ws.request_json(session, "GET", "https://example.test") == {"ok": True}
    assert logins["count"] == 2


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

    result = client.request_json("DELETE", "https://example.test/api/v2/image?Action=DeleteImage")

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
    """The active account TOML is the sole identity source.

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
    """Resolve the session cache through the active InspireSkill account."""
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_account_session_storage,  # noqa: ANN001, ARG001
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
        storage_state={"cookies": []},
        cookies={},
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
        storage_state={"cookies": []},
        created_at=time.time(),
        login_username="alpha-user",
        account="alpha",
    )
    session.save()

    alpha_cache = (
        fake_home / ".inspire" / "accounts" / "alpha" / "web_session.json"
    )
    beta_cache = fake_home / ".inspire" / "accounts" / "beta" / "web_session.json"
    assert json.loads(alpha_cache.read_text())["account"] == "alpha"
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
        storage_state={"cookies": []},
        created_at=time.time(),
        login_username="before",
        account="alpha",
    )
    alpha_cache.write_text(json.dumps(original.to_dict()))

    loaded = WebSession.load()
    assert loaded is not None
    assert loaded.account == "alpha"

    active_account["name"] = "beta"
    loaded.login_username = "after"
    loaded.save()

    beta_cache = fake_home / ".inspire" / "accounts" / "beta" / "web_session.json"
    assert json.loads(alpha_cache.read_text())["login_username"] == "after"
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
        storage_state={"cookies": []},
        created_at=time.time(),
        account="alpha",
    )
    session.save(account="gamma")

    accounts_root = fake_home / ".inspire" / "accounts"
    gamma_cache = accounts_root / "gamma" / "web_session.json"
    assert json.loads(gamma_cache.read_text())["account"] == "gamma"
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
