"""The one gate every CAS credential submission passes through.

CAS locks an account after a handful of rejected logins, and this client logs
in on its own: a session expires mid-command and something above quietly
re-authenticates. That is correct exactly once. What must never happen is one
expiry turning into a burst — the transport retrying in another channel, a
wrapper retrying the whole call, eight concurrent CLI processes each waiting on
the same lock and then each submitting the same password in turn.

Bounding that per call site does not work, because the call sites do not know
about each other. So the bound lives at the only place that actually puts a
password on the wire, and it is persistent and per account: once a submission
has been rejected, the same credentials are refused locally until the cooldown
passes. Waiters find the marker instead of CAS, and a loop that would have run
straight into a lockout gets a local error and a wait time.

The escape hatches are the two things that mean "this attempt is not the one
that just failed": the cooldown expiring, and the credentials changing. The
second is why a fingerprint is stored — someone who retypes their password
after a typo should not be made to wait for a failure that belonged to the old
one, and someone who retypes the *same* password is repeating the submission
this module exists to stop.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from inspire.accounts.cache_lock import exclusive_cache_lock

from .models import AuthenticationError, WebSession, get_session_cache_file

logger = logging.getLogger(__name__)


# How long the same credentials stay refused, by consecutive failure count.
# The first entry only has to outlive one fan-out — a foreground command, the
# background refresher and whatever else woke up on the same expiry. The later
# ones exist because a repeat failure means the credentials themselves are
# wrong, and that is the case where retrying on a timer is what reaches the
# lockout threshold.
COOLDOWN_SCHEDULE_SECONDS = (60.0, 300.0, 900.0, 1800.0)

# Consecutive means consecutive in time as well. A failure nobody followed up
# on for an hour stops escalating, so a bad week does not start the next
# attempt at half an hour.
FAILURE_MEMORY_SECONDS = 3600.0

_BLOCK_FILE_NAME = "web_session.login-block.json"

# The fingerprint answers one question — "are these the credentials that just
# failed?" — and lives next to a config.toml that already holds the password in
# clear text, so it adds no exposure that is not there already. It is still
# derived, not stored, and derived expensively enough that a copy of the marker
# on its own is not a password oracle.
_FINGERPRINT_ITERATIONS = 200_000
_FINGERPRINT_SALT = b"inspire.session.login-guard.v1"


@dataclass(frozen=True)
class _LoginBlock:
    """One recorded credential submission that CAS did not accept."""

    failed_at: float
    blocked_until: float
    failures: int
    fingerprint: str
    session_created_at: float


def cooldown_for(failures: int) -> float:
    """Seconds the same credentials stay refused after *failures* rejections."""
    index = min(max(int(failures), 1), len(COOLDOWN_SCHEDULE_SECONDS)) - 1
    return COOLDOWN_SCHEDULE_SECONDS[index]


def credential_fingerprint(username: str, password: str) -> str:
    """Derive the stored identity of one (username, password) pair."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        _FINGERPRINT_SALT + b"|" + username.encode("utf-8"),
        _FINGERPRINT_ITERATIONS,
    ).hex()


def block_file(account: Optional[str]) -> Path | None:
    """Path of the account's marker, or ``None`` when no account is resolved."""
    cache_file = get_session_cache_file(account)
    if cache_file is None:
        return None
    return cache_file.with_name(_BLOCK_FILE_NAME)


def clear_login_block(account: Optional[str]) -> None:
    """Forget the account's recorded failure — a login has just succeeded."""
    _remove(block_file(account))


def _remove(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("Could not remove login block %s", path, exc_info=True)


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _load(path: Path | None) -> _LoginBlock | None:
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # An unreadable marker must not wedge logins forever, and must not be
        # read as "no failure" either; drop it and let this attempt record its
        # own outcome.
        _remove(path)
        return None

    if not isinstance(payload, dict):
        _remove(path)
        return None
    failed_at = _finite(payload.get("failed_at"))
    blocked_until = _finite(payload.get("blocked_until"))
    failures = _finite(payload.get("failures"))
    fingerprint = payload.get("credential_fingerprint")
    session_created_at = _finite(payload.get("session_created_at")) or 0.0
    if failed_at is None or blocked_until is None or failures is None:
        _remove(path)
        return None
    if not isinstance(fingerprint, str) or not fingerprint:
        _remove(path)
        return None
    return _LoginBlock(
        failed_at=failed_at,
        blocked_until=blocked_until,
        failures=max(1, int(failures)),
        fingerprint=fingerprint,
        session_created_at=session_created_at,
    )


def _store(path: Path | None, block: _LoginBlock) -> None:
    if path is None:
        return
    payload = {
        "failed_at": block.failed_at,
        "blocked_until": block.blocked_until,
        "failures": block.failures,
        "credential_fingerprint": block.fingerprint,
        "session_created_at": block.session_created_at,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        # The attempt in this process still stops. Losing the marker only costs
        # the cross-process part of the guard, and must not replace the real
        # authentication error with a filesystem one.
        logger.debug("Could not record login block %s", path, exc_info=True)


def _forget(
    block: _LoginBlock,
    *,
    account: Optional[str],
    fingerprint: str,
    now: float,
) -> bool:
    """Whether *block* is about something other than the attempt starting now.

    Note what is *not* here: the cooldown running out. That stops the block
    from refusing the attempt, but the record still has to survive it, because
    the failure count it carries is what makes a second rejection wait longer
    than the first.
    """
    if block.fingerprint != fingerprint:
        # Different credentials. Whatever CAS rejected, it was not these.
        return True
    if now < block.failed_at or now - block.failed_at >= FAILURE_MEMORY_SECONDS:
        # Either the clock moved backwards far enough that the recorded
        # deadline cannot be reasoned about, or nothing has retried for long
        # enough that this no longer describes a run of failures.
        return True
    cached = WebSession.load(allow_expired=True, account=account)
    if cached is not None and cached.created_at > block.session_created_at:
        # Some process authenticated and persisted a session after this failure
        # was recorded, then died before clearing it. The session wins.
        return True
    return False


def _blocked_message(block: _LoginBlock, *, now: float) -> str:
    remaining = max(1, math.ceil(block.blocked_until - now))
    attempts = "attempt" if block.failures == 1 else "consecutive attempts"
    return (
        "Automatic login is paused: the platform rejected these exact credentials "
        f"{block.failures} {attempts} ago and no new credentials have been sent since.\n"
        "Repeating the same submission is what locks an account out of CAS, so this "
        f"attempt was stopped locally without contacting the platform. Wait {remaining}s, "
        "or fix the account's login name and password first — corrected credentials are "
        "accepted immediately.\n"
        "Run `inspire account check --details` to see which account and base URL are active, "
        "and `inspire account set --password` (or `inspire init`) to update them."
    )


@contextmanager
def guarded_credential_submission(
    username: str,
    password: str,
    *,
    account: Optional[str],
    now: Optional[float] = None,
) -> Iterator[None]:
    """Wrap the one block of code that submits *password* to CAS.

    Raises :class:`AuthenticationError` instead of running the body when the
    same credentials were just rejected. Records the failure when the body
    raises one, and clears the record when the body returns.
    """
    path = block_file(account)
    if path is None:
        # No account resolved, so there is nowhere to record a failure and
        # nothing for another process to read. Nothing to serialize on either.
        yield
        return
    # Held for the whole login, not just the check: `inspire init` calls
    # `login_with_playwright()` directly, outside the refresh lock, so without
    # this two concurrent runs both read "no failure recorded" and both submit.
    # A separate lock file from the session cache and the refresh lock, so it
    # can never nest with either.
    with exclusive_cache_lock(path):
        yield from _guarded(username, password, path, account=account, now=now)


def _guarded(
    username: str,
    password: str,
    path: Path,
    *,
    account: Optional[str],
    now: Optional[float],
) -> Iterator[None]:
    fingerprint = credential_fingerprint(username, password)
    current = time.time() if now is None else float(now)

    block = _load(path)
    if block is not None and _forget(
        block, account=account, fingerprint=fingerprint, now=current
    ):
        _remove(path)
        block = None
    if block is not None and current < block.blocked_until:
        raise AuthenticationError(_blocked_message(block, now=current))

    try:
        yield
    except AuthenticationError:
        # A rejected login can take most of a minute to come back. The cooldown
        # is measured from when CAS answered, not from when we started asking.
        failed_at = current if now is not None else time.time()
        failures = 1 if block is None else block.failures + 1
        cached = WebSession.load(allow_expired=True, account=account)
        _store(
            path,
            _LoginBlock(
                failed_at=failed_at,
                blocked_until=failed_at + cooldown_for(failures),
                failures=failures,
                fingerprint=fingerprint,
                session_created_at=cached.created_at if cached is not None else 0.0,
            ),
        )
        raise
    else:
        _remove(path)


__all__ = [
    "COOLDOWN_SCHEDULE_SECONDS",
    "FAILURE_MEMORY_SECONDS",
    "block_file",
    "clear_login_block",
    "cooldown_for",
    "credential_fingerprint",
    "guarded_credential_submission",
]
