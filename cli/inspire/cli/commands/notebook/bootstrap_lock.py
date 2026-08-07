"""Serialize the cold notebook connection bootstrap across CLI processes.

One ``ssh`` invocation can spawn several ``ProxyCommand`` processes at once.
Without this lock each would independently observe the tunnel as unavailable
and run its own login and tunnel preparation against the same notebook.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import click

from inspire.accounts import AccountError, account_dir, current_account, inspire_home
from inspire.accounts.cache_lock import CacheLockTimeout, exclusive_cache_lock

from .target_resolver import NotebookConnectionTarget

# Waiters give the preparing process its full setup budget plus room for the
# surrounding login and tunnel handshake before giving up on the lock.
_BOOTSTRAP_WAIT_SLACK = 60.0


def notebook_target_is_ready(
    target: NotebookConnectionTarget | None,
    availability_check: Callable[..., bool],
) -> bool:
    """Report whether *target* already has a reachable tunnel, without retrying."""
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
    setup_timeout: float,
) -> Iterator[None]:
    """Let one process at a time prepare this account's notebook connection.

    A stuck holder must not strand every later ``ssh``, so the wait is bounded
    by *setup_timeout* plus slack; past that the caller proceeds unserialized,
    which is the duplicated-bootstrap behaviour that predates this lock.
    """
    lock_target = _bootstrap_lock_path(account, workspace=workspace, notebook=notebook)
    with ExitStack() as stack:
        try:
            stack.enter_context(
                exclusive_cache_lock(
                    lock_target,
                    timeout=setup_timeout + _BOOTSTRAP_WAIT_SLACK,
                    on_wait=_announce_wait,
                )
            )
        except CacheLockTimeout:
            click.echo(
                "Another process is still preparing this notebook connection; "
                "continuing without waiting.",
                err=True,
            )
        yield


def _bootstrap_lock_path(account: str | None, *, workspace: str, notebook: str) -> Path:
    resolved_account = (account or "").strip() or current_account()
    try:
        lock_root = account_dir(resolved_account) if resolved_account else inspire_home()
    except AccountError:
        # An unusable account name is reported by the resolvers further down;
        # bootstrap still serializes, just not per account.
        lock_root = inspire_home()
    scope = hashlib.sha256(f"{workspace}\0{notebook}".encode()).hexdigest()
    return lock_root / "locks" / f"notebook-bootstrap-{scope}"


def _announce_wait() -> None:
    click.echo(
        "Waiting for another process to prepare the notebook SSH connection...",
        err=True,
    )
