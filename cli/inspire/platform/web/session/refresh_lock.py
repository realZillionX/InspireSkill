"""Cross-process serialization for account web-session refreshes."""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from inspire.accounts.cache_lock import exclusive_cache_lock

from .models import AuthenticationError, WebSession, get_session_cache_file


# Repeated credential submissions can trigger CAS account lockout.  A failed
# refresh therefore needs a cross-process circuit breaker, not another retry
# loop. One minute is enough to collapse one CLI/background-worker fan-out
# while still recovering quickly from a transient authentication fault.
AUTH_FAILURE_COOLDOWN_SECONDS = 60


def _failure_file(account: str | None) -> Path | None:
    cache_file = get_session_cache_file(account)
    if cache_file is None:
        return None
    return cache_file.with_name("web_session.auth-failure.json")


def _remove_failure_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def clear_session_auth_failure(account: str | None) -> None:
    """Clear the failed-login circuit after a successful refresh."""
    _remove_failure_file(_failure_file(account))


def record_session_auth_failure(
    account: str | None,
    *,
    session_created_at: float | None,
    now: float | None = None,
) -> None:
    """Persist one failed credential submission for concurrent CLI processes."""
    path = _failure_file(account)
    if path is None:
        return
    failed_at = time.time() if now is None else float(now)
    payload = {
        "failed_at": failed_at,
        "session_created_at": float(session_created_at or 0.0),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError:
        # The in-process attempt still stops.  Failure to persist the breaker
        # must not hide the original authentication error.
        pass


def _read_failure(path: Path) -> tuple[float, float] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        failed_at = float(payload["failed_at"])
        session_created_at = float(payload.get("session_created_at") or 0.0)
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        _remove_failure_file(path)
        return None
    if not math.isfinite(failed_at) or not math.isfinite(session_created_at):
        _remove_failure_file(path)
        return None
    return failed_at, session_created_at


def raise_if_session_auth_blocked(
    account: str | None,
    *,
    now: float | None = None,
) -> None:
    """Fail locally while the account's failed-login circuit is open."""
    path = _failure_file(account)
    if path is None or not path.exists():
        return
    failure = _read_failure(path)
    if failure is None:
        return
    failed_at, failed_session_created_at = failure
    current_time = time.time() if now is None else float(now)

    if current_time - failed_at >= AUTH_FAILURE_COOLDOWN_SECONDS:
        _remove_failure_file(path)
        return

    # Editing account credentials is an explicit recovery action.  Do not
    # make the user wait out a breaker created for the previous config.
    config_file = path.parent / "config.toml"
    try:
        if config_file.stat().st_mtime > failed_at:
            _remove_failure_file(path)
            return
    except OSError:
        pass

    # A successful process may have saved the replacement Session and crashed
    # before removing the marker.  The newer Session is authoritative.
    cached = WebSession.load(allow_expired=True, account=account)
    if cached is not None and cached.created_at > failed_session_created_at:
        _remove_failure_file(path)
        return

    remaining = max(1, math.ceil(AUTH_FAILURE_COOLDOWN_SECONDS - (current_time - failed_at)))
    raise AuthenticationError(
        "Automatic login is paused because the previous credential submission did not "
        "complete. No new credentials were sent. To protect the account from CAS lockout, "
        f"wait about {remaining} seconds or update the account credentials before retrying once."
    )


@contextmanager
def exclusive_session_refresh(account: str | None) -> Iterator[None]:
    """Serialize login refreshes for one account without locking API requests."""
    cache_file = get_session_cache_file(account)
    if cache_file is None or not cache_file.parent.is_dir():
        yield
        return

    refresh_target = cache_file.with_name(f"{cache_file.name}.refresh")
    with exclusive_cache_lock(refresh_target):
        yield


__all__ = [
    "AUTH_FAILURE_COOLDOWN_SECONDS",
    "clear_session_auth_failure",
    "exclusive_session_refresh",
    "raise_if_session_auth_blocked",
    "record_session_auth_failure",
]
