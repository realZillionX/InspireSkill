"""Resolve every web API call to its caller-owned or process-default transport."""

from __future__ import annotations

import atexit
import os
import threading
from contextvars import ContextVar
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from inspire.platform.web.session.models import WebSession
    from inspire.platform.web.transport import Transport

active_transport: ContextVar[Transport | None] = ContextVar("inspire_transport", default=None)
# Installed by the CLI for its Click lifetime; the platform never imports Click.
session_transport: ContextVar[Callable[[WebSession | None], Transport] | None] = ContextVar(
    "inspire_session_transport", default=None
)
_default_transports: dict[tuple[int, str | None, str], Transport] = {}
_default_lock = threading.RLock()


def new_default_transport(account: str | None, base_url: str) -> Transport:
    from inspire.platform.web.transport import Transport

    return Transport(account, base_url, username="", allow_browser=True, cli_compat=True)


def get_transport(session: WebSession | None = None) -> Transport:
    """Resolve identity before acquisition; adopting a supplied session never logs in."""
    from inspire.accounts import current_account
    from inspire.platform.web.browser_api.core import _configured_base_url

    active = active_transport.get()
    if active is not None and (session is None or not active.cli_compat):
        return active
    adopt = session_transport.get()
    if adopt is not None:
        return adopt(session)
    if active is not None:
        return active
    account = session.account if session is not None and session.account else current_account()
    base_url = _configured_base_url().rstrip("/")
    key = (os.getpid(), account, base_url)
    with _default_lock:
        transport = _default_transports.get(key)
        if transport is None:
            transport = new_default_transport(account, base_url)
            _default_transports[key] = transport
        if session is not None:
            transport.adopt_session(session)
        return transport


def close_default_transports() -> None:
    with _default_lock:
        transports = list(_default_transports.values())
        _default_transports.clear()
    for transport in transports:
        if transport._pid == os.getpid():
            transport.close()


atexit.register(close_default_transports)
