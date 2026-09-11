"""Acquire, persist and renew platform sessions for the owning Transport.

CAS and browser authentication, cookie jars, refresh locks and login cooldowns
belong here. Request retries and single-send write policy belong to
inspire.platform.web.transport_core; endpoint payloads belong to
inspire.platform.web.browser_api. A locally fresh cache is not proof that the
server still accepts its cookies.
"""


from __future__ import annotations

import sys
from inspire.platform.web.flow import Program, workflow, call, enter_context, exit_context

import atexit
import logging
from pathlib import Path
from typing import Optional

from inspire.config.models import DEFAULT_BASE_URL
from inspire.platform.web.session.browser_client import _BrowserRequestClient  # noqa: F401
from inspire.platform.web.session.browser_client import (
    _close_browser_client,
    _get_browser_client as _get_browser_client,
)
from inspire.platform.web.session.auth import (
    get_credentials as _get_credentials,
    get_web_session as _get_web_session,
    login_with_playwright as _login_with_playwright,
    renew_web_session_without_credentials as _renew_web_session_without_credentials,  # noqa: F401
)
from inspire.platform.web.session.models import (
    AuthenticationError,
    DEFAULT_WORKSPACE_ID,
    SESSION_TTL,
    TRANSIENT_HTTP_STATUSES,
    SessionExpiredError,
    TransientAPIError,
    WebSession,
    get_session_cache_file,
    is_transient_api_error,
)
from inspire.platform.web.session.browser_launch import (
    is_playwright_browser_runtime_error as is_playwright_browser_runtime_error,
    playwright_install_hint,
)
from inspire.platform.web.session.proxy import get_playwright_proxy
from inspire.platform.web.session.refresh_lock import exclusive_session_refresh
from inspire.platform.web.session.requests import (
    build_requests_session,
    close_pooled_requests_session,
    pooled_requests_session,
)
from inspire.platform.web.session.retry import (
    retry_after_seconds as retry_after_seconds,
    with_transient_retry,
)

__all__ = [
    "refresh_session_in_place",
    "raise_browser_runtime_error",
    "acquire_web_session",
    "renew_web_session_without_credentials",
    "close_browser_client",
    "get_browser_client",
    "create_browser_client",
    "AuthenticationError",
    "DEFAULT_WORKSPACE_ID",
    "SESSION_TTL",
    "TRANSIENT_HTTP_STATUSES",
    "SessionExpiredError",
    "TransientAPIError",
    "WebSession",
    "build_requests_session",
    "clear_all_session_caches",
    "clear_session_cache",
    "close_pooled_requests_session",
    "get_credentials",
    "get_playwright_proxy",
    "get_web_session",
    "is_transient_api_error",
    "login_with_playwright",
    "pooled_requests_session",
    "with_transient_retry",
]


logger = logging.getLogger(__name__)


atexit.register(_close_browser_client)


def raise_browser_runtime_error(exc: BaseException) -> None:
    raise RuntimeError(
        "Playwright Chromium could not start for Inspire web requests. Prepare "
        "the standard CLI runtime with:\n"
        f"    {playwright_install_hint()}\n"
        "Then retry the command."
    ) from exc


def refresh_session_in_place(current: "WebSession", refreshed: "WebSession") -> None:
    """Replace an existing session object's fields with refreshed credentials/state."""
    current.storage_state = refreshed.storage_state
    current.cookies = refreshed.cookies
    current.workspace_id = refreshed.workspace_id
    current.login_username = refreshed.login_username
    current.base_url = refreshed.base_url
    current.user_detail = refreshed.user_detail
    current.all_workspace_ids = refreshed.all_workspace_ids
    current.all_workspace_names = refreshed.all_workspace_names
    current.all_workspace_fair_scheduling = refreshed.all_workspace_fair_scheduling
    current.created_at = refreshed.created_at


# Resolve legacy patch hooks at call time; transport callers use public names.
def create_browser_client(session: WebSession) -> _BrowserRequestClient:
    return _BrowserRequestClient(session)


def get_browser_client(session: WebSession) -> _BrowserRequestClient:
    return _get_browser_client(session)


def close_browser_client() -> None:
    _close_browser_client()


@workflow
def renew_web_session_without_credentials(session: WebSession) -> Program[WebSession | None]:
    return (yield call(_renew_web_session_without_credentials, session))


@workflow
def acquire_web_session(
    force_refresh: bool = False,
    require_workspace: bool = False,
    account: Optional[str] = None,
) -> Program[WebSession]:
    """Acquire without the outer refresh lock or front-end adoption callback.

    A transport rebuilding its existing session already owns the refresh lock
    and must update that object in place before exposing it to the caller.
    """
    return (yield call(_get_web_session,
        force_refresh=force_refresh, require_workspace=require_workspace, account=account
    ))


def get_credentials() -> tuple[str, str]:
    return _get_credentials()


def login_with_playwright(
    username: str,
    password: str,
    base_url: str = DEFAULT_BASE_URL,
    headless: bool = True,
) -> WebSession:
    return _login_with_playwright(
        username,
        password,
        base_url=base_url,
        headless=headless,
    )


@workflow
def get_web_session(
    force_refresh: bool = False,
    require_workspace: bool = False,
    account: Optional[str] = None,
) -> Program[WebSession]:
    # The lock covers *force_refresh* too. That is the call that logs in, so
    # skipping it was letting every concurrent process past the one gate meant
    # to make them share a single refresh.
    context = exclusive_session_refresh(account)
    yield call(enter_context, context)
    try:
        session = yield call(_get_web_session,
            force_refresh=force_refresh,
            require_workspace=require_workspace,
            account=account,
        )
    finally:
        yield call(exit_context, context, *sys.exc_info())
    from inspire.platform.web.runtime import session_transport

    adopt = session_transport.get()
    if adopt is not None:
        adopt(session)
    return session


def _remove_session_file(session_file: Path | None) -> None:
    if session_file is None or not session_file.exists():
        return
    try:
        session_file.unlink()
    except Exception:
        return


def clear_session_cache(
    account: str | None = None,
    *,
    all_accounts: bool = False,
) -> None:
    """Remove cached Web session for one account.

    By default this clears the active account only. Switching accounts and
    refreshing an expired session must not delete another account's session,
    because the Agent may switch back to that account immediately.
    """
    if not all_accounts:
        _remove_session_file(get_session_cache_file(account))
        return

    clear_all_session_caches()


def clear_all_session_caches() -> None:
    """Remove every ``~/.inspire/accounts/*/web_session.json``."""
    accounts_root = Path.home() / ".inspire" / "accounts"
    if not accounts_root.exists():
        return
    for account_dir in accounts_root.iterdir():
        if not account_dir.is_dir():
            continue
        _remove_session_file(account_dir / "web_session.json")
