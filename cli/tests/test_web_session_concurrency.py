"""Rotating refresh tokens are consumed only once across CLI processes."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from inspire.platform.web.session import WebSession
from inspire.platform.web.session import auth
from inspire.platform.web.session.oauth import TokenResponse
from multiprocess_workers import Barrier, Counter, adopt_home, run_workers, worker_context

WORKER_COUNT = 8


def _refresh(home: str, barrier: Barrier, counter: Counter, forced: bool) -> None:
    adopt_home(home)
    cached = WebSession.load(allow_expired=True, account="default")
    assert cached is not None

    def rotate(self, refresh_token: str) -> TokenResponse:
        assert refresh_token == "old-refresh"
        with counter.get_lock():
            counter.value += 1
        time.sleep(0.2)
        return TokenResponse("new-access", "new-refresh", time.time()+3600, time.time()+6000)

    auth.DeviceAuthorization.refresh = rotate
    barrier.wait()
    result = auth.refresh_session(cached) if forced else auth.get_web_session(account="default")
    assert result.access_token == "new-access"
    assert result.refresh_token == "new-refresh"


@pytest.mark.parametrize("forced", [False, True])
def test_processes_share_one_rotating_refresh(monkeypatch, tmp_path, forced):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cached = WebSession(access_token="old-access", refresh_token="old-refresh",
                        access_expires_at=time.time()+(-10 if not forced else 3600),
                        refresh_expires_at=time.time()+6000, created_at=1,
                        account="default", base_url="https://example.test")
    cached.save()
    context = worker_context()
    barrier = context.Barrier(WORKER_COUNT)
    counter = context.Value("i", 0)
    codes = run_workers(context, _refresh, count=WORKER_COUNT,
                        args_for=lambda _: (str(tmp_path), barrier, counter, forced))
    assert codes == [0] * WORKER_COUNT
    assert counter.value == 1
    assert WebSession.load(account="default").refresh_token == "new-refresh"
    cached.save()
    assert WebSession.load(account="default").refresh_token == "new-refresh"
