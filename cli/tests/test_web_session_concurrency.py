from __future__ import annotations

import multiprocessing
import os
import threading
import time
from pathlib import Path
from typing import Protocol

from inspire.platform.web import session as web_session_module
from inspire.platform.web.session import WebSession
from inspire.platform.web.session import auth as web_session_auth

WORKER_COUNT = 8


class _Barrier(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...


class _CounterLock(Protocol):
    def __enter__(self) -> None: ...

    def __exit__(self, *_args) -> None: ...  # noqa: ANN002


class _Counter(Protocol):
    value: int

    def get_lock(self) -> _CounterLock: ...


class _SessionBrowserClient:
    def __init__(self, session: WebSession) -> None:
        self._session = session

    def request_json(self, *_args, **_kwargs) -> dict[str, bool]:  # noqa: ANN002, ANN003
        if self._session.created_at == 1.0:
            raise web_session_module.SessionExpiredError("expired")
        return {"ok": True}


def _refresh_expired_session(
    home: str,
    barrier: _Barrier,
    login_count: _Counter,
) -> None:
    os.environ["HOME"] = home
    session = WebSession.load(allow_expired=True, account="default")
    assert session is not None

    def fake_get_web_session(**_kwargs) -> WebSession:  # noqa: ANN003
        with login_count.get_lock():
            login_count.value += 1
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
    web_session_module.get_web_session = fake_get_web_session

    barrier.wait()
    assert web_session_module.request_json(
        session,
        "GET",
        "https://example.test/api/v1/user/detail",
    ) == {"ok": True}


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
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(WORKER_COUNT)
    login_count = context.Value("i", 0)
    workers = [
        context.Process(
            target=_refresh_expired_session,
            args=(str(tmp_path), barrier, login_count),
        )
        for _ in range(WORKER_COUNT)
    ]

    # When: every process discovers the expiry at the same time.
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    # Then: one process logs in and the waiters reuse its refreshed session.
    assert [worker.exitcode for worker in workers] == [0] * WORKER_COUNT
    assert login_count.value == 1


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
    barrier: _Barrier,
    login_count: _Counter,
) -> None:
    os.environ["HOME"] = home

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
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(WORKER_COUNT)
    login_count = context.Value("i", 0)
    workers = [
        context.Process(
            target=_load_or_login_session,
            args=(str(tmp_path), barrier, login_count),
        )
        for _ in range(WORKER_COUNT)
    ]

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert [worker.exitcode for worker in workers] == [0] * WORKER_COUNT
    assert login_count.value == 1
