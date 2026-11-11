"""Cross-process serialization for account web-session refreshes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from .models import get_session_cache_file


@contextmanager
def exclusive_session_refresh(account: str | None, *, timeout: float | None = None) -> Iterator[None]:
    """Serialize login refreshes for one account without locking API requests."""
    cache_file = get_session_cache_file(account)
    if cache_file is None:
        yield
        return

    from inspire.accounts.storage import account_lifecycle_lock

    with account_lifecycle_lock(cache_file.parent.name, timeout=timeout):
        yield
