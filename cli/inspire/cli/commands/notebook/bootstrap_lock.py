from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from inspire.accounts import account_dir, current_account, inspire_home
from inspire.accounts.cache_lock import exclusive_cache_lock
from .target_resolver import NotebookConnectionTarget


def notebook_target_is_ready(
    target: NotebookConnectionTarget | None,
    availability_check: Callable[..., bool],
) -> bool:
    if target is None:
        return False
    return availability_check(
        bridge_name=target.bridge.name,
        config=target.config,
        retries=0,
        retry_pause=0.0,
        progressive=False,
    )


@contextmanager
def serialize_notebook_bootstrap(
    *,
    account: str | None,
    workspace: str,
    notebook: str,
) -> Iterator[None]:
    resolved_account = (account or "").strip() or current_account()
    lock_root = account_dir(resolved_account) if resolved_account else inspire_home()
    scope = hashlib.sha256(f"{workspace}\0{notebook}".encode()).hexdigest()
    lock_target = lock_root / "locks" / f"notebook-bootstrap-{scope}"
    with exclusive_cache_lock(lock_target):
        yield
