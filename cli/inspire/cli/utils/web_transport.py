"""Own the lazy web transport for one Click invocation."""

from __future__ import annotations

import threading

import click

from inspire.accounts import current_account
from inspire.platform.web.runtime import new_default_transport, session_transport
from inspire.platform.web.session.models import WebSession
from inspire.platform.web.transport import Transport


def install_web_transport() -> None:
    ctx = click.get_current_context()
    transports: dict[tuple[str | None, str], Transport] = {}
    lock = threading.RLock()

    def adopt(session: WebSession | None) -> Transport:
        from inspire.platform.web.browser_api.core import _configured_base_url
        from inspire.platform.web.runtime import active_transport

        account = session.account if session is not None and session.account else current_account()
        base_url = _configured_base_url().rstrip("/")
        key = (account, base_url)
        with lock:
            transport = transports.get(key)
            if transport is None:
                transport = new_default_transport(account, base_url)
                transports[key] = transport
                ctx.call_on_close(transport.close)
            if session is not None:
                transport.adopt_session(session)
        if (
            active_transport.get() is not transport
            and click.get_current_context(silent=True) is not None
        ):
            scope = transport.scope(timeout=None)
            scope.__enter__()
            ctx.call_on_close(lambda: scope.__exit__(None, None, None))
        return transport

    token = session_transport.set(adopt)
    ctx.call_on_close(lambda: session_transport.reset(token))
