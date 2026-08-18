"""Cross-process serialization for account web-session refreshes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from inspire.accounts.cache_lock import exclusive_cache_lock

from .models import get_session_cache_file


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
