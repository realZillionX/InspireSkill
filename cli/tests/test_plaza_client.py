"""数据广场 sign-in handshake and transport discipline."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

import pytest

from inspire.platform.web.plaza import core as plaza_core
from inspire.platform.web.session import SessionExpiredError, TransientAPIError
from inspire.platform.web.session import retry as retry_module
from inspire.platform.web.runtime import get_transport
from inspire.platform.web.transport import Transport


class _FakeWebSession:
    account = "tester"
    created_at = 1000.0
    storage_state: dict[str, Any] = {"cookies": [{"name": "CASTGC", "value": "TGT-1"}]}


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: Any = None,
        headers: Optional[dict[str, str]] = None,
        json_error: bool = False,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self._json_error = json_error

    def json(self) -> Any:
        if self._json_error:
            raise ValueError("not json")
        return self._payload


class _FakeHTTP:
    """Stands in for the proxy-aware ``requests.Session`` the CLI builds."""

    def __init__(self, script: "_Script") -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self._script = script

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self._script.cas()

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self._script.login()

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._script.api()

    def close(self) -> None:
        self.closed = True


class _Script:
    """The responses one test wants, in the order the client asks for them."""

    def __init__(
        self,
        *,
        cas: Optional[list[_FakeResponse]] = None,
        login: Optional[list[_FakeResponse]] = None,
        api: Optional[list[_FakeResponse]] = None,
    ) -> None:
        self._cas = cas or []
        self._login = login or []
        self._api = api or []
        self.sessions: list[_FakeHTTP] = []
        self.sign_ins = 0

    def _next(self, queue: list[_FakeResponse], default: _FakeResponse) -> _FakeResponse:
        return queue.pop(0) if queue else default

    def cas(self) -> _FakeResponse:
        self.sign_ins += 1
        return self._next(
            self._cas,
            _FakeResponse(
                status_code=302,
                headers={"Location": "https://aip.sii.edu.cn/?ticket=ST-1-abc"},
            ),
        )

    def login(self) -> _FakeResponse:
        return self._next(
            self._login,
            _FakeResponse(payload={"code": 0, "data": {"userInfo": {"ID": 2734}}}),
        )

    def api(self) -> _FakeResponse:
        return self._next(self._api, _FakeResponse(payload={"code": 0, "data": {"ok": True}}))


@pytest.fixture(autouse=True)
def _reset_plaza_client(monkeypatch):
    transport = Transport("tester", "https://qz.sii.edu.cn", username="", cli_compat=True)
    transport.adopt_session(_FakeWebSession())
    monkeypatch.setattr(transport, "_refresh", lambda: None)
    with transport.scope(timeout=None):
        yield
    transport.close()


def _install(monkeypatch: pytest.MonkeyPatch, script: _Script) -> _Script:
    def _build(session: Any, base_url: str) -> _FakeHTTP:  # noqa: ANN401
        del session, base_url
        http = _FakeHTTP(script)
        script.sessions.append(http)
        return http

    monkeypatch.setattr(plaza_core, "build_requests_session", _build)
    return script


def test_sign_in_spends_a_cas_ticket_on_the_plaza(monkeypatch) -> None:  # noqa: ANN001
    script = _install(monkeypatch, _Script())

    plaza_core.plaza_request("GET", "/api/datasets/getDatasetsList", params={"page": 1})

    http = script.sessions[0]
    cas_call, login_call, api_call = http.calls
    service = quote("https://aip.sii.edu.cn/", safe="")
    assert cas_call["url"] == f"https://cas.sii.edu.cn/cas/login?service={service}"
    # Following the redirect spends the ticket on the SPA's own index page.
    assert cas_call["allow_redirects"] is False
    assert login_call["url"] == "https://aip.sii.edu.cn/api/base/login"
    assert login_call["json"] == {
        "ticket": "ST-1-abc",
        "service": "https://aip.sii.edu.cn/",
    }
    assert http.headers["x-user-id"] == "2734"
    assert api_call["url"] == "https://aip.sii.edu.cn/api/datasets/getDatasetsList"
    assert api_call["params"] == {"page": 1}
    assert api_call["allow_redirects"] is False


def test_plaza_request_returns_the_unwrapped_data(monkeypatch) -> None:  # noqa: ANN001
    _install(
        monkeypatch,
        _Script(api=[_FakeResponse(payload={"code": 0, "data": {"total": 531}, "msg": "获取成功"})]),
    )

    assert plaza_core.plaza_request("GET", "/api/datasets/getDatasetsList") == {"total": 531}


def test_plaza_request_signs_in_once_per_transport(monkeypatch) -> None:  # noqa: ANN001
    script = _install(monkeypatch, _Script())

    plaza_core.plaza_request("GET", "/api/datasets/getDatasetsList")
    plaza_core.plaza_request("GET", "/api/datasetTags/getDatasetTagsList")

    assert script.sign_ins == 1
    assert len(script.sessions) == 1


def test_a_declared_failure_carries_the_platform_reason(monkeypatch) -> None:  # noqa: ANN001
    _install(
        monkeypatch,
        _Script(api=[_FakeResponse(payload={"code": 7, "data": {}, "msg": "查询失败:record not found"})]),
    )

    with pytest.raises(plaza_core.PlazaError, match="record not found"):
        plaza_core.plaza_request("POST", "/api/datasets/findDatasets", body={"datasetId": 1})


def test_an_expired_plaza_cookie_is_re_minted_without_a_platform_login(monkeypatch) -> None:  # noqa: ANN001
    script = _install(
        monkeypatch,
        _Script(
            api=[
                _FakeResponse(
                    status_code=401,
                    payload={"code": 7, "data": None, "msg": "未登录或非法访问"},
                ),
                _FakeResponse(payload={"code": 0, "data": {"total": 1}}),
            ]
        ),
    )
    refreshes: list[bool] = []
    monkeypatch.setattr(
        get_transport(),
        "_refresh",
        lambda: refreshes.append(True),
    )

    assert plaza_core.plaza_request("GET", "/api/datasets/getDatasetsList") == {"total": 1}
    assert script.sign_ins == 2
    # The CAS cookie was fine; only the plaza's own session had lapsed.
    assert refreshes == []
    assert script.sessions[0].closed is True


def test_a_dead_cas_cookie_forces_one_platform_refresh(monkeypatch) -> None:  # noqa: ANN001
    script = _install(
        monkeypatch,
        _Script(
            cas=[
                _FakeResponse(status_code=302, headers={"Location": "https://cas/login"}),
                _FakeResponse(status_code=302, headers={"Location": "https://cas/login"}),
                _FakeResponse(
                    status_code=302,
                    headers={"Location": "https://aip.sii.edu.cn/?ticket=ST-2-def"},
                ),
            ]
        ),
    )
    refreshes: list[bool] = []
    monkeypatch.setattr(
        get_transport(),
        "_refresh",
        lambda *, require_cas_ticket: refreshes.append(require_cas_ticket),
    )

    assert plaza_core.plaza_request("GET", "/api/datasets/getDatasetsList") == {"ok": True}
    assert refreshes == [True]
    assert script.sign_ins == 3


def test_a_session_that_never_authenticates_is_reported_as_expired(monkeypatch) -> None:  # noqa: ANN001
    _install(
        monkeypatch,
        _Script(
            api=[
                _FakeResponse(status_code=401, payload={"code": 7, "msg": "未登录或非法访问"}),
                _FakeResponse(status_code=401, payload={"code": 7, "msg": "未登录或非法访问"}),
                _FakeResponse(status_code=401, payload={"code": 7, "msg": "未登录或非法访问"}),
            ]
        ),
    )

    with pytest.raises(SessionExpiredError):
        plaza_core.plaza_request("GET", "/api/datasets/getDatasetsList")


def test_a_login_redirect_counts_as_not_signed_in(monkeypatch) -> None:  # noqa: ANN001
    script = _install(
        monkeypatch,
        _Script(
            api=[
                _FakeResponse(status_code=302, headers={"Location": "https://cas/login"}),
                _FakeResponse(payload={"code": 0, "data": {"total": 2}}),
            ]
        ),
    )

    assert plaza_core.plaza_request("GET", "/api/datasets/getDatasetsList") == {"total": 2}
    assert script.sign_ins == 2


def test_throttling_is_waited_out_rather_than_read_as_an_answer(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(retry_module, "backoff_delay", lambda *_args, **_kwargs: 0.0)
    script = _install(
        monkeypatch,
        _Script(
            api=[
                _FakeResponse(status_code=429, headers={"Retry-After": "0"}),
                _FakeResponse(payload={"code": 0, "data": {"total": 3}}),
            ]
        ),
    )

    assert plaza_core.plaza_request("GET", "/api/datasets/getDatasetsList") == {"total": 3}
    # A rate limiter says nothing about the data, so the session is kept.
    assert script.sign_ins == 1


def test_persistent_throttling_surfaces_as_a_transient_error(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(retry_module, "backoff_delay", lambda *_args, **_kwargs: 0.0)
    _install(
        monkeypatch,
        _Script(api=[_FakeResponse(status_code=429) for _ in range(retry_module.MAX_ATTEMPTS)]),
    )

    with pytest.raises(TransientAPIError):
        plaza_core.plaza_request("GET", "/api/datasets/getDatasetsList")


def test_a_non_json_body_is_not_mistaken_for_an_empty_answer(monkeypatch) -> None:  # noqa: ANN001
    _install(monkeypatch, _Script(api=[_FakeResponse(json_error=True)]))

    with pytest.raises(plaza_core.PlazaError, match="non-JSON"):
        plaza_core.plaza_request("GET", "/api/datasets/getDatasetsList")


def test_reset_closes_the_cached_session(monkeypatch) -> None:  # noqa: ANN001
    script = _install(monkeypatch, _Script())

    plaza_core.plaza_request("GET", "/api/datasets/getDatasetsList")
    plaza_core.reset_plaza_client()

    assert script.sessions[0].closed is True
    plaza_core.plaza_request("GET", "/api/datasets/getDatasetsList")
    assert script.sign_ins == 2


@pytest.fixture
def sdk_client(tmp_path, monkeypatch):
    from pathlib import Path
    from inspire.sdk import InspireClient
    from inspire.platform.web.session.models import WebSession

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    account = tmp_path / ".inspire" / "accounts" / "tester"
    account.mkdir(parents=True)
    (account / "config.toml").write_text('[auth]\nusername="test"\npassword="unused"\n')
    client = InspireClient(account="tester", allow_browser=False)
    client._transport.adopt_session(WebSession(
        storage_state=_FakeWebSession.storage_state,
        created_at=1000, account="tester", base_url=client.base_url,
    ))
    yield client
    client.close()


def test_sdk_clients_own_separate_plaza_sessions(sdk_client, monkeypatch):
    from inspire.sdk import InspireClient

    script = _install(monkeypatch, _Script())
    with InspireClient(account="tester", allow_browser=False) as other:
        other._transport.adopt_session(sdk_client._transport.session)
        sdk_client.datasets.tags()
        other.datasets.tags()
        assert script.sign_ins == 2
        assert script.sessions[0] is not script.sessions[1]
        sdk_client.close()
        assert script.sessions[0].closed
        assert not script.sessions[1].closed
        other.datasets.tags()
        assert script.sign_ins == 2
    assert script.sessions[1].closed


@pytest.mark.parametrize("login_fails", [False, True])
def test_expired_sdk_session_uses_browser_free_refresh(sdk_client, monkeypatch, login_fails):
    from contextlib import nullcontext
    from inspire.sdk import AuthenticationError
    from inspire.platform.web.session import auth, refresh_lock
    from inspire.platform.web.session.models import WebSession

    script = _install(monkeypatch, _Script(cas=[_FakeResponse(), _FakeResponse()]))
    steps = []
    monkeypatch.setattr(refresh_lock, "exclusive_session_refresh", lambda *a, **k: nullcontext())
    monkeypatch.setattr(WebSession, "load", lambda **k: None)
    monkeypatch.setattr(auth, "renew_web_session_without_credentials",
                        lambda session: steps.append("renew"))
    monkeypatch.setattr(auth, "get_credentials", lambda account: ("test", "unused"))

    def login(*args, **kwargs):
        steps.append("http-login")
        if login_fails:
            raise ValueError("CAS requires verification")
        return sdk_client._transport.session

    def browser(*args, **kwargs):
        pytest.fail("Browser login must never be reached")

    monkeypatch.setattr(auth, "login_without_browser", login)
    monkeypatch.setattr(auth, "get_web_session", browser)
    monkeypatch.setattr(auth, "login_with_playwright", browser)
    if login_fails:
        with pytest.raises(AuthenticationError, match="verification"):
            sdk_client.datasets.tags()
    else:
        assert sdk_client.datasets.tags() == ()
        assert script.sign_ins == 3
    assert steps == ["http-login"]


def test_second_plaza_401_escalates_once(monkeypatch):
    script = _install(monkeypatch, _Script(api=[
        _FakeResponse(status_code=401, payload={"code": 7}),
        _FakeResponse(status_code=401, payload={"code": 7}),
    ]))
    observed = []
    monkeypatch.setattr(get_transport(), "_refresh", lambda: observed.append(script.sign_ins))
    assert plaza_core.plaza_request("GET", "/read") == {"ok": True}
    assert observed == [2]
    assert script.sign_ins == 3


@pytest.mark.parametrize("stage", ["cas", "login", "api", "retry"])
def test_sdk_plaza_deadline_bounds_every_step(sdk_client, monkeypatch, stage):
    import requests
    from inspire.sdk import WaitTimeoutError
    from inspire.platform.web import transport as transport_module

    clock = [100.0]
    monkeypatch.setattr(transport_module.time, "monotonic", lambda: clock[0])
    sdk_client.operation_timeout = 0.01
    script = _install(monkeypatch, _Script())

    def exhaust():
        clock[0] += 0.02
        raise requests.Timeout("budget spent")

    if stage == "retry":
        script._api = [_FakeResponse(status_code=429, headers={"Retry-After": "8"})]
        waits = []

        def sleep(delay):
            waits.append(delay)
            clock[0] += delay

        monkeypatch.setattr(transport_module.time, "sleep", sleep)
    else:
        monkeypatch.setattr(script, stage, exhaust)
    with pytest.raises(WaitTimeoutError):
        sdk_client.datasets.tags()
    assert script.sign_ins <= 1
    for http in script.sessions:
        for call in http.calls:
            assert 0 < call["timeout"] <= 0.010001
    if stage == "retry":
        assert waits == pytest.approx([0.01])
        assert len(script.sessions[0].calls) == 3


@pytest.mark.parametrize("response,expected", [
    (_FakeResponse(payload={"code": 7, "msg": "denied"}), "ValidationError"),
    (_FakeResponse(status_code=403), "AuthenticationError"),
    (_FakeResponse(status_code=429), "TransportError"),
    (_FakeResponse(status_code=503), "MutationUncertainError"),
    (_FakeResponse(status_code=501), "MutationUncertainError"),
    (_FakeResponse(status_code=401, payload={"code": 7}), "MutationUncertainError"),
    (_FakeResponse(json_error=True), "MutationUncertainError"),
])
def test_single_send_plaza_classification(sdk_client, monkeypatch, response, expected):
    import time
    from inspire.sdk import exceptions

    script = _install(monkeypatch, _Script(api=[response]))
    transport = sdk_client._transport
    transport._last_success = time.monotonic()
    with transport.scope(), pytest.raises(getattr(exceptions, expected)), transport.single_send():
        plaza_core.plaza_request("POST", "/write", body={"value": 1})
    assert script.sign_ins == 1
    assert len(script.sessions[0].calls) == 3


def test_single_send_handshake_failure_does_not_mark_write_sent(sdk_client, monkeypatch):
    import time

    script = _install(monkeypatch, _Script(login=[_FakeResponse(json_error=True)]))
    transport = sdk_client._transport
    transport._last_success = time.monotonic()
    with transport.scope(), pytest.raises(plaza_core.PlazaError), transport.single_send():
        plaza_core.plaza_request("POST", "/write")
    assert len(script.sessions[0].calls) == 2
    assert script.sessions[0].closed


def test_bare_plaza_uses_shared_process_default(monkeypatch):
    from inspire.platform.web import runtime
    from inspire.platform.web import session
    _install(monkeypatch, _Script())
    monkeypatch.setattr("inspire.accounts.current_account", lambda: None)
    monkeypatch.setattr("inspire.platform.web.browser_api.core._configured_base_url",
                        lambda: "https://qz.sii.edu.cn")
    monkeypatch.setattr(session, "get_web_session", lambda **kwargs: _FakeWebSession())
    token = runtime.active_transport.set(None)
    try:
        plaza_core.plaza_request("GET", "/read")
        transport = runtime.get_transport()
        assert transport._plaza_slot.client is not None
        plaza_core.reset_plaza_client()
        assert transport._plaza_slot.client is None
    finally:
        runtime.active_transport.reset(token)


def test_refresh_http_step_uses_remaining_sdk_budget(sdk_client, monkeypatch):
    import requests
    from inspire.platform.web.session import auth, refresh_lock
    from inspire.platform.web.session.models import WebSession
    from inspire.platform.web import transport as transport_module
    from inspire.sdk import WaitTimeoutError
    from contextlib import nullcontext

    clock = [100.0]
    monkeypatch.setattr(transport_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(refresh_lock, "exclusive_session_refresh", lambda *a, **k: nullcontext())
    monkeypatch.setattr(WebSession, "load", lambda **k: None)
    monkeypatch.setattr(auth, "renew_web_session_without_credentials", lambda session: None)
    monkeypatch.setattr(auth, "get_credentials", lambda account: ("test", "unused"))
    _install(monkeypatch, _Script(cas=[_FakeResponse(), _FakeResponse()]))
    timeouts = []

    def get(http, url, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] += 0.02
        raise requests.Timeout("authentication budget spent")

    monkeypatch.setattr(requests.Session, "get", get)
    sdk_client.operation_timeout = 0.01
    with pytest.raises(WaitTimeoutError):
        sdk_client.datasets.tags()
    assert timeouts == pytest.approx([0.01])


def test_single_send_plaza_disconnect_is_uncertain(sdk_client, monkeypatch):
    import requests
    import time
    from inspire.sdk import SubmissionUncertainError

    script = _install(monkeypatch, _Script())

    def disconnect():
        raise requests.ConnectionError("response lost")

    monkeypatch.setattr(script, "api", disconnect)
    transport = sdk_client._transport
    transport._last_success = time.monotonic()
    with transport.scope(), pytest.raises(SubmissionUncertainError), transport.single_send(create=True):
        plaza_core.plaza_request("POST", "/write")
    assert len(script.sessions[0].calls) == 3


def test_plaza_and_platform_share_single_send_budget(sdk_client, monkeypatch):
    import time
    from inspire.platform.web.transport import _SingleSendViolation

    script = _install(monkeypatch, _Script())
    transport = sdk_client._transport
    transport._last_success = time.monotonic()
    with transport.scope(), pytest.raises(_SingleSendViolation), transport.single_send():
        plaza_core.plaza_request("POST", "/write")
        transport.request("POST", "/second-write")
    assert len(script.sessions[0].calls) == 3


@pytest.mark.parametrize("stage", [
    "plaza_start", "plaza_response", "plaza_exception",
    "signin_cas", "signin_login", "signin_exception",
    "refresh_cache", "refresh_renew", "refresh_login", "refresh_outer",
])
def test_check_deadline_preserves_discarded_remaining_error(monkeypatch, stage):
    from contextlib import nullcontext
    import requests
    from inspire.platform.errors import WaitTimeoutError
    from inspire.platform.web.session import auth, refresh_lock
    from inspire.platform.web.session.models import WebSession

    transport = get_transport()
    script = _install(monkeypatch, _Script())
    transport.deadline = 0
    with pytest.raises(WaitTimeoutError) as original:
        transport.remaining()
    with pytest.raises(type(original.value)) as explicit:
        transport.check_deadline()
    assert type(explicit.value) is type(original.value)
    assert str(explicit.value) == str(original.value)
    transport.deadline = None
    checked = []
    check = transport.check_deadline

    def record_check():
        if transport.deadline == 0:
            checked.append(stage)
        check()

    def expire(response=None, *, error=False):
        transport.deadline = 0
        if error:
            raise requests.Timeout("budget spent")
        return response

    monkeypatch.setattr(transport, "check_deadline", record_check)
    if stage.startswith("refresh_"):
        monkeypatch.setattr(refresh_lock, "exclusive_session_refresh", lambda *a, **k: nullcontext())
        monkeypatch.setattr(WebSession, "load", lambda **k: None)
        monkeypatch.setattr(auth, "renew_web_session_without_credentials", lambda s: None)
        monkeypatch.setattr(auth, "get_credentials", lambda a: ("test", "unused"))
        if stage == "refresh_cache":
            monkeypatch.setattr(WebSession, "load", lambda **k: expire(error=True))
        elif stage == "refresh_renew":
            monkeypatch.setattr(auth, "renew_web_session_without_credentials",
                                lambda s: expire(error=True))
        elif stage == "refresh_login":
            monkeypatch.setattr(auth, "login_without_browser", lambda *a, **k: expire(error=True))
        else:
            monkeypatch.setattr(refresh_lock, "exclusive_session_refresh",
                                lambda *a, **k: expire(error=True))
        def action():
            Transport._refresh(transport)
    else:
        if stage == "plaza_start":
            transport.deadline = 0
        elif stage in ("plaza_response", "plaza_exception"):
            monkeypatch.setattr(script, "api", lambda: expire(
                _FakeResponse(payload={"code": 0}), error=stage == "plaza_exception"))
        elif stage == "signin_cas":
            monkeypatch.setattr(script, "cas", lambda: expire(_FakeResponse()))
        elif stage == "signin_login":
            monkeypatch.setattr(script, "login", lambda: expire(_FakeResponse()))
        else:
            monkeypatch.setattr(script, "cas", lambda: expire(error=True))
        def action():
            transport.plaza_request("GET", "/read")
    with pytest.raises(type(original.value)) as actual:
        action()
    assert type(actual.value) is type(original.value)
    assert str(actual.value) == str(original.value)
    assert checked


def test_plaza_backoff_does_not_block_ordinary_request(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from inspire.platform.web import transport as transport_module

    transport = get_transport()
    _install(monkeypatch, _Script(api=[_FakeResponse(status_code=429)]))
    sleeping, release = Event(), Event()

    def sleep(delay):
        sleeping.set()
        assert release.wait(5)

    monkeypatch.setattr(transport_module.time, "sleep", sleep)
    monkeypatch.setattr(transport, "_once", lambda *a, **k: {"ordinary": True})
    with ThreadPoolExecutor(max_workers=2) as pool:
        plaza = pool.submit(transport.plaza_request, "GET", "/read")
        try:
            assert sleeping.wait(5)
            ordinary = pool.submit(transport.request, "GET", "/ordinary")
            assert ordinary.result(timeout=2) == {"ordinary": True}
            assert not plaza.done()
        finally:
            release.set()
        assert plaza.result(timeout=5) == {"ok": True}


def test_plaza_reset_waits_for_cookie_jar_borrower(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    transport = get_transport()
    script = _install(monkeypatch, _Script())
    entered, release, resetting = Event(), Event(), Event()
    closed = []
    monkeypatch.setattr(_FakeHTTP, "close", lambda self: closed.append(self))

    def api():
        entered.set()
        assert release.wait(5)
        assert closed == []
        return _FakeResponse(payload={"code": 0, "data": {"ok": True}})

    def reset():
        resetting.set()
        transport.reset_plaza_client()

    monkeypatch.setattr(script, "api", api)
    with ThreadPoolExecutor(max_workers=3) as pool:
        call = pool.submit(transport.plaza_request, "GET", "/read")
        try:
            assert entered.wait(5)
            first = pool.submit(reset)
            assert resetting.wait(5)
            second = pool.submit(reset)
            assert not first.done()
        finally:
            release.set()
        assert call.result(timeout=5) == {"ok": True}
        first.result(timeout=5)
        second.result(timeout=5)
    assert closed == script.sessions
    assert transport._plaza_slot.client is None


def test_concurrent_plaza_calls_exclusively_borrow_one_cookie_jar(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    transport = get_transport()
    script = _install(monkeypatch, _Script())
    entered, release, started = Event(), Event(), Event()
    active = []

    def api():
        assert active == []
        active.append(True)
        entered.set()
        assert release.wait(5)
        active.pop()
        return _FakeResponse(payload={"code": 0, "data": {"ok": True}})

    def second_call():
        started.set()
        return transport.plaza_request("GET", "/second")

    monkeypatch.setattr(script, "api", api)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(transport.plaza_request, "GET", "/first")
        try:
            assert entered.wait(5)
            second = pool.submit(second_call)
            assert started.wait(5)
            assert not second.done()
            # Neither the shared generation lock nor plaza state lock spans I/O.
            assert transport._generation_lock.acquire(blocking=False)
            transport._generation_lock.release()
            assert transport._plaza_slot.lock.acquire(blocking=False)
            transport._plaza_slot.lock.release()
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5) == {"ok": True}
    assert script.sign_ins == 1
    assert len(script.sessions) == 1
