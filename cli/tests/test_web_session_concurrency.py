from __future__ import annotations

import threading
import time
from pathlib import Path

from inspire.platform.web import session as web_session_module
from inspire.platform.web.session import WebSession
from inspire.platform.web.session import auth as web_session_auth
from multiprocess_workers import (
    Barrier,
    Counter,
    adopt_home,
    run_workers,
    worker_context,
)

WORKER_COUNT = 8

_http_calls: list[str] = []


class _SessionBrowserClient:
    def __init__(self, session: WebSession) -> None:
        self._session = session

    def request_json(self, *_args, **_kwargs) -> dict[str, bool]:  # noqa: ANN002, ANN003
        if self._session.created_at == 1.0:
            raise web_session_module.SessionExpiredError("expired")
        return {"ok": True}


class _StubResponse:
    status_code = 200
    text = ""

    def json(self) -> dict[str, bool]:
        return {"ok": True}


class _StubHTTP:
    def get(  # noqa: ANN001
        self, url: str, headers=None, timeout=None, allow_redirects=True
    ) -> _StubResponse:
        assert allow_redirects is False
        _http_calls.append(url)
        return _StubResponse()

    def close(self) -> None:
        pass


def _refresh_expired_session(
    home: str,
    barrier: Barrier,
    login_count: Counter,
) -> None:
    adopt_home(home)
    session = WebSession.load(allow_expired=True, account="default")
    assert session is not None

    def fake_get_web_session(**_kwargs) -> WebSession:  # noqa: ANN003
        with login_count.get_lock():
            login_count.value += 1
        # A real CAS login is slow. Hold that window open so an unserialized
        # waiter would read the still-stale cache and start its own login.
        time.sleep(0.2)
        refreshed = WebSession(
            storage_state={
                "cookies": [{"name": "session", "value": "fresh"}],
                "origins": [],
            },
            cookies={"session": "fresh"},
            account="default",
            created_at=2.0,
        )
        refreshed.save(account="default")
        return refreshed

    web_session_module._BROWSER_API_FORCE_BROWSER = True
    web_session_module._get_browser_client = _SessionBrowserClient
    web_session_module._close_browser_client = lambda: None
    web_session_module._get_web_session = fake_get_web_session
    web_session_module.pooled_requests_session = lambda _session, _url: _StubHTTP()

    barrier.wait()
    assert web_session_module.request_json(
        session,
        "GET",
        "https://example.test/api/v2/user?Action=GetUserDetail",
    ) == {"ok": True}
    # Reauthentication must return the process to the plain HTTP request path
    # rather than leaving it pinned to the browser fallback.
    assert _http_calls == ["https://example.test/api/v2/user?Action=GetUserDetail"]


def test_concurrent_expired_session_requests_share_one_refresh(
    monkeypatch,
    tmp_path: Path,
) -> None:  # noqa: ANN001
    # Given: independent CLI processes loaded the same expired account session.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    old_session = WebSession(
        storage_state={
            "cookies": [{"name": "session", "value": "expired"}],
            "origins": [],
        },
        cookies={"session": "expired"},
        account="default",
        created_at=1.0,
    )
    old_session.save(account="default")
    context = worker_context()
    barrier = context.Barrier(WORKER_COUNT)
    login_count = context.Value("i", 0)

    # When: every process discovers the expiry at the same time.
    exit_codes = run_workers(
        context,
        _refresh_expired_session,
        count=WORKER_COUNT,
        args_for=lambda _index: (str(tmp_path), barrier, login_count),
    )

    # Then: one process logs in and the waiters reuse its refreshed session.
    assert exit_codes == [0] * WORKER_COUNT
    assert login_count.value == 1


def _reject_expired_session_refresh(
    home: str,
    barrier: Barrier,
    submissions: Counter,
) -> None:
    """One CLI process meeting an expired session whose re-login is refused."""
    adopt_home(home)
    session = WebSession.load(allow_expired=True, account="default")
    assert session is not None

    def reject(*_args, **_kwargs) -> WebSession:  # noqa: ANN002, ANN003
        with submissions.get_lock():
            submissions.value += 1
        # A rejected CAS login is not fast. Hold the window open so a waiter
        # that is not guarded would get past the lock and submit its own.
        time.sleep(0.2)
        raise web_session_module.AuthenticationError("CAS rejected the login")

    # Patched below the guard, not around it: the point of the test is that the
    # guard is what stops the other seven, not the caller.
    web_session_auth._submit_credentials = reject
    web_session_module._BROWSER_API_FORCE_BROWSER = True
    web_session_module._get_browser_client = _SessionBrowserClient
    web_session_module._close_browser_client = lambda: None
    web_session_module.pooled_requests_session = lambda _session, _url: _StubHTTP()

    barrier.wait()
    try:
        web_session_module.request_json(
            session,
            "GET",
            "https://example.test/api/v2/user?Action=GetUserDetail",
        )
    except web_session_module.AuthenticationError:
        return
    raise AssertionError("a refused login must stop the request")


def test_concurrent_failed_refresh_submits_credentials_only_once(
    monkeypatch,
    tmp_path: Path,
) -> None:  # noqa: ANN001
    """Eight processes, one expired session, one wrong password.

    This is the shape that reaches a CAS lockout: the account-level lock only
    ever de-duplicated *successful* refreshes, so the seven waiters behind a
    failed one each went on to submit the same credentials in turn.
    """
    # Given: the account whose session expired, and credentials CAS will refuse.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    account_path = tmp_path / ".inspire" / "accounts" / "default"
    account_path.mkdir(parents=True)
    (account_path / "config.toml").write_text(
        "[auth]\nusername = 'user'\npassword = 'wrong-secret'\n",
        encoding="utf-8",
    )
    (tmp_path / ".inspire" / "current").write_text("default\n", encoding="utf-8")
    WebSession(
        storage_state={
            "cookies": [{"name": "session", "value": "expired"}],
            "origins": [],
        },
        cookies={"session": "expired"},
        account="default",
        created_at=1.0,
    ).save(account="default")
    context = worker_context()
    barrier = context.Barrier(WORKER_COUNT)
    submissions = context.Value("i", 0)

    # When: every process discovers the expiry at the same moment.
    exit_codes = run_workers(
        context,
        _reject_expired_session_refresh,
        count=WORKER_COUNT,
        args_for=lambda _index: (str(tmp_path), barrier, submissions),
    )

    # Then: the password reached CAS once, and the other seven never left the
    # machine.
    assert exit_codes == [0] * WORKER_COUNT
    assert submissions.value == 1
    assert (account_path / "web_session.login-block.json").exists()
    # The rejected session stays: it is the generation marker the marker itself
    # refers to, and clearing it up front left a failed login with nothing on
    # disk to tell the next process the attempt had already happened.
    assert (account_path / "web_session.json").exists()


def _login_directly(home: str, barrier: Barrier, submissions: Counter) -> None:
    """The `inspire init` shape: `login_with_playwright()` with no refresh lock."""
    adopt_home(home)

    def reject(*_args, **_kwargs) -> WebSession:  # noqa: ANN002, ANN003
        with submissions.get_lock():
            submissions.value += 1
        time.sleep(0.2)
        raise web_session_module.AuthenticationError("CAS rejected the login")

    web_session_auth._submit_credentials = reject

    barrier.wait()
    try:
        web_session_auth.login_with_playwright(
            "user",
            "wrong-secret",
            base_url="https://qz.sii.edu.cn",
            account="default",
        )
    except web_session_module.AuthenticationError:
        return
    raise AssertionError("a refused login must not return a session")


def test_concurrent_direct_logins_submit_credentials_only_once(
    monkeypatch,
    tmp_path: Path,
) -> None:  # noqa: ANN001
    """`init` calls the submission point directly, outside the refresh lock.

    The account-level refresh lock covers every path that goes through
    `get_web_session()`, which is almost all of them — but `inspire init` calls
    `login_with_playwright()` itself, so the guard has to serialize on its own
    marker rather than borrow someone else's lock.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".inspire" / "accounts" / "default").mkdir(parents=True)
    context = worker_context()
    barrier = context.Barrier(WORKER_COUNT)
    submissions = context.Value("i", 0)

    exit_codes = run_workers(
        context,
        _login_directly,
        count=WORKER_COUNT,
        args_for=lambda _index: (str(tmp_path), barrier, submissions),
    )

    assert exit_codes == [0] * WORKER_COUNT
    assert submissions.value == 1


def test_stale_session_save_does_not_replace_refreshed_cache(
    monkeypatch,
    tmp_path: Path,
) -> None:  # noqa: ANN001
    # Given: another process already persisted a newer authenticated session.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    refreshed = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "fresh"}]},
        account="default",
        created_at=2.0,
    )
    stale = WebSession(
        storage_state={"cookies": [{"name": "session", "value": "expired"}]},
        account="default",
        created_at=1.0,
    )
    refreshed.save(account="default")

    # When: an older in-flight request attempts to save its session afterward.
    stale.save(account="default")

    # Then: the refreshed credentials remain in the shared account cache.
    cached = WebSession.load(allow_expired=True, account="default")
    assert cached is not None
    assert cached.created_at == 2.0
    assert cached.cookies == {"session": "fresh"}


def _load_or_login_session(
    home: str,
    barrier: Barrier,
    login_count: Counter,
) -> None:
    adopt_home(home)

    def fake_login(
        username: str,
        password: str,
        base_url: str = "",
        headless: bool = True,
        account: str | None = None,
    ) -> WebSession:
        del password, base_url, headless
        try:
            barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        with login_count.get_lock():
            login_count.value += 1
        time.sleep(0.05)
        refreshed = WebSession(
            storage_state={
                "cookies": [{"name": "session", "value": "fresh"}],
                "origins": [],
            },
            cookies={"session": "fresh"},
            login_username=username,
            account=account,
            created_at=time.time(),
        )
        refreshed.save(account=account)
        return refreshed

    web_session_auth.get_credentials = lambda _account=None: ("new-user", "secret")
    web_session_auth._load_runtime_config = lambda _account=None: type(
        "Config", (), {"base_url": "https://example.test"}
    )()
    web_session_auth.login_with_playwright = fake_login
    barrier.wait()

    session = web_session_module.get_web_session(account="default")
    assert session.login_username == "new-user"


def test_concurrent_initial_session_login_shares_one_refresh(
    monkeypatch,
    tmp_path: Path,
) -> None:  # noqa: ANN001
    # Given: independent cold CLI processes find the same stale account session.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    stale = WebSession(
        storage_state={
            "cookies": [{"name": "session", "value": "expired"}],
            "origins": [],
        },
        cookies={"session": "expired"},
        login_username="old-user",
        account="default",
        created_at=1.0,
    )
    stale.save(account="default")
    context = worker_context()
    barrier = context.Barrier(WORKER_COUNT)
    login_count = context.Value("i", 0)

    # When: every process reaches the login decision at the same time.
    exit_codes = run_workers(
        context,
        _load_or_login_session,
        count=WORKER_COUNT,
        args_for=lambda _index: (str(tmp_path), barrier, login_count),
    )

    # Then: one process logs in and the waiters reuse the session it cached.
    assert exit_codes == [0] * WORKER_COUNT
    assert login_count.value == 1
