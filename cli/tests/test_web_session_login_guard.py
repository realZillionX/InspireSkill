"""The credential-submission guard: what it refuses, and what it lets through.

The property under test is not "logins fail gracefully" but "the same rejected
password is never put on the wire twice on its own". Everything here is either
a way that could happen, or a legitimate retry that must still get through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from inspire.platform.web.session import AuthenticationError, WebSession
from inspire.platform.web.session import login_guard


@pytest.fixture(autouse=True)
def _account_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    account_dir = tmp_path / ".inspire" / "accounts" / "alice"
    account_dir.mkdir(parents=True)
    (account_dir / "config.toml").write_text("[auth]\nusername = 'alice'\n", encoding="utf-8")
    return account_dir


def _attempt(
    *,
    password: str = "secret",
    now: float | None = None,
    fail: bool = True,
) -> None:
    with login_guard.guarded_credential_submission("alice", password, account="alice", now=now):
        if fail:
            raise AuthenticationError("CAS rejected the login")


def test_second_submission_of_rejected_credentials_never_reaches_cas() -> None:
    with pytest.raises(AuthenticationError, match="CAS rejected"):
        _attempt(now=100.0)

    submitted = False
    with pytest.raises(AuthenticationError, match="Automatic login is paused"):
        with login_guard.guarded_credential_submission(
            "alice", "secret", account="alice", now=101.0
        ):
            submitted = True

    assert submitted is False


def test_corrected_credentials_are_accepted_immediately() -> None:
    with pytest.raises(AuthenticationError):
        _attempt(password="typo", now=100.0)

    # Same account, same second, different password: this is the user fixing
    # what was wrong, not a retry of what failed.
    reached_cas = False
    with login_guard.guarded_credential_submission(
        "alice", "corrected", account="alice", now=100.0
    ):
        reached_cas = True

    assert reached_cas is True


def test_a_successful_login_clears_the_record() -> None:
    with pytest.raises(AuthenticationError):
        _attempt(now=100.0)
    # The record outlives its own cooldown so a second rejection waits longer
    # than the first; only an accepted login retires it.
    assert login_guard.block_file("alice").exists()

    accepted_at = 100.0 + login_guard.COOLDOWN_SCHEDULE_SECONDS[0]
    with login_guard.guarded_credential_submission(
        "alice", "secret", account="alice", now=accepted_at
    ):
        pass

    assert not login_guard.block_file("alice").exists()


def test_cooldown_grows_with_consecutive_rejections() -> None:
    schedule = login_guard.COOLDOWN_SCHEDULE_SECONDS
    at = 100.0
    for expected in schedule:
        with pytest.raises(AuthenticationError, match="CAS rejected"):
            _attempt(now=at)
        blocked_until = at + expected
        with pytest.raises(AuthenticationError, match="Automatic login is paused"):
            _attempt(now=blocked_until - 1)
        at = blocked_until

    # Capped, not unbounded: the last entry repeats for every further failure.
    with pytest.raises(AuthenticationError, match="CAS rejected"):
        _attempt(now=at)
    with pytest.raises(AuthenticationError, match="Automatic login is paused"):
        _attempt(now=at + schedule[-1] - 1)
    with pytest.raises(AuthenticationError, match="CAS rejected"):
        _attempt(now=at + schedule[-1])


def test_a_failure_nobody_followed_up_on_stops_escalating() -> None:
    with pytest.raises(AuthenticationError, match="CAS rejected"):
        _attempt(now=100.0)

    stale = 100.0 + login_guard.FAILURE_MEMORY_SECONDS
    with pytest.raises(AuthenticationError, match="CAS rejected"):
        _attempt(now=stale)

    # Back to the first step of the schedule, not the second.
    with pytest.raises(AuthenticationError, match="Automatic login is paused"):
        _attempt(now=stale + login_guard.COOLDOWN_SCHEDULE_SECONDS[0] - 1)
    with pytest.raises(AuthenticationError, match="CAS rejected"):
        _attempt(now=stale + login_guard.COOLDOWN_SCHEDULE_SECONDS[0])


def test_a_session_saved_after_the_failure_reopens_logins() -> None:
    with pytest.raises(AuthenticationError):
        _attempt(now=100.0)

    # Some other process authenticated and persisted its session, then died
    # before clearing the marker. The session is the newer fact.
    WebSession(
        storage_state={"cookies": [{"name": "session", "value": "fresh"}], "origins": []},
        account="alice",
        created_at=200.0,
    ).save(account="alice")

    reached_cas = False
    with login_guard.guarded_credential_submission(
        "alice", "secret", account="alice", now=101.0
    ):
        reached_cas = True

    assert reached_cas is True


def test_the_marker_never_stores_the_credentials() -> None:
    with pytest.raises(AuthenticationError):
        _attempt(password="hunter2-plaintext", now=100.0)

    raw = login_guard.block_file("alice").read_text(encoding="utf-8")
    assert "hunter2-plaintext" not in raw
    assert "alice" not in raw


def test_an_unreadable_marker_does_not_wedge_logins() -> None:
    login_guard.block_file("alice").write_text("{not json", encoding="utf-8")

    reached_cas = False
    with login_guard.guarded_credential_submission("alice", "secret", account="alice"):
        reached_cas = True

    assert reached_cas is True


def test_failures_that_are_not_authentication_failures_are_not_recorded() -> None:
    with pytest.raises(RuntimeError):
        with login_guard.guarded_credential_submission("alice", "secret", account="alice"):
            raise RuntimeError("Chromium could not start")

    assert not login_guard.block_file("alice").exists()


def test_the_block_is_per_account() -> None:
    other = Path.home() / ".inspire" / "accounts" / "bob"
    other.mkdir(parents=True)

    with pytest.raises(AuthenticationError):
        _attempt(now=100.0)

    reached_cas = False
    with login_guard.guarded_credential_submission("bob", "secret", account="bob", now=101.0):
        reached_cas = True

    assert reached_cas is True
