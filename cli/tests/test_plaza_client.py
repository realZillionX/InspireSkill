"""数据广场 sign-in handshake and transport discipline."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

import pytest

from inspire.platform.web.plaza import core as plaza_core
from inspire.platform.web.session import SessionExpiredError, TransientAPIError
from inspire.platform.web.session import retry as retry_module


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
def _reset_plaza_client():  # noqa: ANN201
    plaza_core.reset_plaza_client()
    yield
    plaza_core.reset_plaza_client()


def _install(monkeypatch: pytest.MonkeyPatch, script: _Script) -> _Script:
    def _build(session: Any, base_url: str) -> _FakeHTTP:  # noqa: ANN401
        del session, base_url
        http = _FakeHTTP(script)
        script.sessions.append(http)
        return http

    monkeypatch.setattr(plaza_core, "build_requests_session", _build)
    monkeypatch.setattr(plaza_core, "get_web_session", lambda **_kwargs: _FakeWebSession())
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


def test_plaza_request_signs_in_once_for_the_whole_process(monkeypatch) -> None:  # noqa: ANN001
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
        plaza_core,
        "get_web_session",
        lambda **kwargs: (refreshes.append(bool(kwargs.get("force_refresh"))), _FakeWebSession())[1],
    )

    assert plaza_core.plaza_request("GET", "/api/datasets/getDatasetsList") == {"total": 1}
    assert script.sign_ins == 2
    # The CAS cookie was fine; only the plaza's own session had lapsed.
    assert refreshes == [False]
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
        plaza_core,
        "get_web_session",
        lambda **kwargs: (refreshes.append(bool(kwargs.get("force_refresh"))), _FakeWebSession())[1],
    )

    assert plaza_core.plaza_request("GET", "/api/datasets/getDatasetsList") == {"ok": True}
    assert refreshes == [False, True]
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
