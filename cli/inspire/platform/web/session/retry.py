"""Waiting out a platform that is momentarily refusing to answer.

The browser APIs answer one ``(workspace, compute group)`` per request, so
every workspace-wide question — the quota catalog, resource availability, node
dimensions — is a fan-out of N requests issued back to back. That shape walks
into the platform's rate limiter, and a single ``429`` in the middle of a
fan-out used to be indistinguishable from "this group has nothing".

Retrying here, at the one transport choke point both the requests and the
Playwright path funnel through, keeps that burst from ever reaching the code
that has to decide what a response means. What survives the retries is a real
outage, and the caller is told so rather than handed an empty list.
"""

from __future__ import annotations

import email.utils
import random
import time
from typing import Any, Callable, Mapping, TypeVar

from .models import TransientAPIError

# Three attempts total. A rate limiter that is still refusing after two waits
# is not a burst, and a CLI command must not sit on it for minutes.
MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 8.0

_T = TypeVar("_T")


def retry_after_seconds(headers: Mapping[str, Any] | None) -> float | None:
    """Read ``Retry-After`` as seconds, in either of the forms RFC 9110 allows.

    Returns ``None`` when the header is absent or unparseable, which leaves the
    caller on its own backoff schedule.
    """
    if not headers:
        return None
    raw = ""
    for key, value in headers.items():
        if str(key).lower() == "retry-after":
            raw = str(value or "").strip()
            break
    if not raw:
        return None

    try:
        return max(0.0, float(raw))
    except ValueError:
        pass

    try:
        deadline = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if deadline is None:
        return None
    return max(0.0, deadline.timestamp() - time.time())


def backoff_delay(attempt: int, error: TransientAPIError) -> float:
    """Seconds to wait before attempt ``attempt + 1`` (0-indexed attempts).

    The platform's own ``Retry-After`` wins when it asks for a wait this side
    is willing to sit through; otherwise it is exponential with jitter, so a
    fan-out that tripped the limiter does not resume in lockstep.
    """
    requested = error.retry_after
    if requested is not None and 0 <= requested <= MAX_BACKOFF_SECONDS:
        return float(requested)
    delay = min(BASE_BACKOFF_SECONDS * (2**attempt), MAX_BACKOFF_SECONDS)
    return delay * (0.75 + random.random() * 0.5)  # noqa: S311 - jitter, not crypto


def with_transient_retry(
    call: Callable[[], _T],
    *,
    max_attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] | None = None,
) -> _T:
    """Run *call*, waiting out transient refusals, and re-raise the last one."""
    attempts = max(1, int(max_attempts))
    for attempt in range(attempts):
        try:
            return call()
        except TransientAPIError as error:
            if attempt == attempts - 1:
                raise
            # Resolved per call, not bound as a default, so the wait is
            # observable in tests without any of them actually sleeping.
            wait = time.sleep if sleep is None else sleep
            wait(backoff_delay(attempt, error))
    raise AssertionError("unreachable")  # pragma: no cover


__all__ = [
    "BASE_BACKOFF_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_BACKOFF_SECONDS",
    "backoff_delay",
    "retry_after_seconds",
    "with_transient_retry",
]
