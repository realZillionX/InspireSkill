"""Web session management for web UI APIs."""

from __future__ import annotations

import atexit
import logging
import threading
from pathlib import Path
from typing import Optional

import requests as requests_lib

from inspire.config.models import DEFAULT_BASE_URL
from inspire.platform.web.session.browser_client import _BrowserRequestClient  # noqa: F401
from inspire.platform.web.session.browser_client import (
    _close_browser_client,
    _get_browser_client,
)
from inspire.platform.web.session.auth import (
    get_credentials as _get_credentials,
    get_web_session as _get_web_session,
    login_with_playwright as _login_with_playwright,
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
    is_playwright_browser_runtime_error,
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
    retry_after_seconds,
    with_transient_retry,
)

__all__ = [
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
    "request_json",
    "with_transient_retry",
]


_BROWSER_API_FORCE_BROWSER = False
logger = logging.getLogger(__name__)


atexit.register(_close_browser_client)


def _raise_browser_runtime_error(exc: BaseException) -> None:
    raise RuntimeError(
        "Playwright Chromium could not start for Inspire web requests. Prepare "
        "the standard CLI runtime with:\n"
        f"    {playwright_install_hint()}\n"
        "Then retry the command."
    ) from exc


def _refresh_session_in_place(current: "WebSession", refreshed: "WebSession") -> None:
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


class _AuthRefreshBudget:
    """One session rebuild per :func:`request_json` call, retries included.

    ``with_transient_retry`` runs the call again after a 429, and a rebuild is
    the one thing that must not be repeated per attempt: it is where a login
    happens. Threading the allowance through the retries — rather than
    resetting it on each one — is what keeps a rate-limited fan-out from
    turning into three logins.
    """

    __slots__ = ("_remaining",)

    def __init__(self, remaining: int) -> None:
        self._remaining = remaining

    def consume(self) -> bool:
        if self._remaining <= 0:
            return False
        self._remaining -= 1
        return True


# The generation a rebuild produced that no call has answered with yet.
#
# The per-call budget bounds rebuilds inside one `request_json`, and a *failed*
# login is bounded by the credential guard. Neither covers the case where the
# login keeps succeeding and the session it mints keeps being refused: the
# budget is fresh for the next call, the guard sees nothing wrong, and a
# workspace-wide fan-out is dozens of calls. Measured at 5 logins for 5 calls.
_unproven_rebuild: float | None = None
_unproven_rebuild_lock = threading.Lock()


def _note_rebuilt_generation(created_at: float) -> None:
    global _unproven_rebuild

    with _unproven_rebuild_lock:
        _unproven_rebuild = created_at


def _note_generation_answered(created_at: float) -> None:
    """A call came back with data, so this generation is usable after all."""
    global _unproven_rebuild

    with _unproven_rebuild_lock:
        if _unproven_rebuild is not None and created_at >= _unproven_rebuild:
            _unproven_rebuild = None


def _would_replace_an_unusable_login(observed_created_at: float) -> bool:
    with _unproven_rebuild_lock:
        unproven = _unproven_rebuild
    return unproven is not None and observed_created_at >= unproven


def _refresh_expired_session(
    session: "WebSession",
    *,
    observed_created_at: float,
) -> "WebSession":
    """Return a session newer than the one whose cookies were just refused.

    *observed_created_at* is the generation the caller actually sent. Comparing
    against that, rather than against ``session.created_at`` as it reads now,
    is what stops a second login when someone else refreshed the shared session
    object mid-flight: the caller's 401 belongs to a generation that has
    already been replaced, and the replacement was never tried.
    """
    account = session.account
    if session.created_at > observed_created_at:
        return session
    with exclusive_session_refresh(account):
        if session.created_at > observed_created_at:
            return session
        cached = WebSession.load(allow_expired=True, account=account)
        if (
            cached is not None
            and bool(cached.storage_state.get("cookies"))
            and cached.created_at > observed_created_at
        ):
            return cached
        # The rejected session stays on disk until its replacement is written:
        # it is the generation marker every other waiter compares against, and
        # deleting it up front means a failed login leaves nothing behind to
        # tell them the attempt already happened.
        return _get_web_session(force_refresh=True, account=account)


def _rebuild_session_and_repeat(
    session: "WebSession",
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]],
    body: Optional[dict],
    timeout: int,
    budget: "_AuthRefreshBudget",
    observed_created_at: float,
) -> dict:
    """The single authentication boundary every browser API call passes."""
    global _BROWSER_API_FORCE_BROWSER

    _close_browser_client()
    if _would_replace_an_unusable_login(observed_created_at):
        raise SessionExpiredError(
            "The session the last rebuild produced was refused as well. Not logging "
            "in again to replace a login nothing has been able to use."
        )
    if not budget.consume():
        raise SessionExpiredError(
            "Session expired again after a single authentication refresh"
        )
    logger.debug("Web session expired; rebuilding it once for this call.")
    new_session = _refresh_expired_session(session, observed_created_at=observed_created_at)
    _refresh_session_in_place(session, new_session)
    _note_rebuilt_generation(session.created_at)
    _BROWSER_API_FORCE_BROWSER = False
    return _request_json_once(
        session,
        method,
        url,
        headers=headers,
        body=body,
        timeout=timeout,
        _budget=budget,
    )


def request_json(
    session: "WebSession",
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    body: Optional[dict] = None,
    timeout: int = 30,
) -> dict:
    """Call one browser API endpoint, waiting out momentary refusals.

    A workspace-wide question costs one request per compute group, which is
    exactly the shape a rate limiter reacts to. Absorbing that here means the
    ``429`` never reaches the callers that have to decide what a response
    means; what does reach them is either data or a
    :class:`TransientAPIError` saying the platform never answered.
    """
    budget = _AuthRefreshBudget(1)
    return with_transient_retry(
        lambda: _request_json_once(
            session,
            method,
            url,
            headers=headers,
            body=body,
            timeout=timeout,
            _budget=budget,
        )
    )


def _request_json_once(
    session: "WebSession",
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    body: Optional[dict] = None,
    timeout: int = 30,
    _budget: Optional["_AuthRefreshBudget"] = None,
) -> dict:
    global _BROWSER_API_FORCE_BROWSER

    budget = _AuthRefreshBudget(1) if _budget is None else _budget
    # Read before the request goes out: it names the session generation whose
    # cookies this call is about to send, and another thread may replace it in
    # place while the request is in flight.
    observed_created_at = session.created_at

    if not _BROWSER_API_FORCE_BROWSER:
        # Pooled, not per-request: this is the path every browser API call
        # takes, and a fresh session here would hand back the connection after
        # each one. See `pooled_requests_session` for what that costs.
        http = pooled_requests_session(session, url)
        try:
            method_upper = method.upper()
            req_headers = headers or {}
            # Redirects are never followed. An unauthenticated call is answered
            # with a 302 towards CAS, and following it turns that signal into an
            # HTML login page — a 200 the caller cannot tell from data.
            if method_upper == "GET":
                resp = http.get(
                    url,
                    headers=req_headers,
                    timeout=timeout,
                    allow_redirects=False,
                )
            elif method_upper == "POST":
                req_headers = dict(req_headers)
                req_headers["Content-Type"] = "application/json"
                resp = http.post(
                    url,
                    headers=req_headers,
                    json=body or {},
                    timeout=timeout,
                    allow_redirects=False,
                )
            elif method_upper == "DELETE":
                resp = http.delete(
                    url,
                    headers=req_headers,
                    timeout=timeout,
                    allow_redirects=False,
                )
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            if resp.status_code == 401 or 300 <= resp.status_code < 400:
                raise SessionExpiredError("Session expired or invalid")
            if resp.status_code >= 400:
                message = f"API returned {resp.status_code}: {resp.text}"
                if resp.status_code in TRANSIENT_HTTP_STATUSES:
                    raise TransientAPIError(
                        message,
                        status=resp.status_code,
                        retry_after=retry_after_seconds(resp.headers),
                    )
                raise ValueError(message)
            try:
                payload = resp.json()
            except ValueError:
                # With redirects disabled this is no longer how an expiry
                # arrives, so it is not treated as one. Let the other transport
                # say what it is; that costs no credentials either way.
                _BROWSER_API_FORCE_BROWSER = True
            else:
                # The generation that answered is the one this call sent, read
                # before the request went out. `session.created_at` as it reads
                # *now* may already be a replacement another thread minted --
                # crediting that one would clear the unproven marker for a
                # login nothing has used, which re-arms exactly the repeated
                # logins the marker exists to stop.
                _note_generation_answered(observed_created_at)
                return payload
        except SessionExpiredError:
            return _rebuild_session_and_repeat(
                session,
                method,
                url,
                headers=headers,
                body=body,
                timeout=timeout,
                budget=budget,
                observed_created_at=observed_created_at,
            )
        except requests_lib.exceptions.RequestException:
            _BROWSER_API_FORCE_BROWSER = True

    from inspire.platform.web.browser_api.core import _in_asyncio_loop, _run_in_thread

    def _browser_request_in_thread() -> dict:
        """Disposable client per thread — avoids cross-thread greenlet errors."""
        client = _BrowserRequestClient(session)
        try:
            return client.request_json(
                method,
                url,
                headers=headers,
                body=body,
                timeout=timeout,
            )
        finally:
            client.close()

    try:
        if _in_asyncio_loop():
            payload = _run_in_thread(_browser_request_in_thread)
        else:
            client = _get_browser_client(session)
            payload = client.request_json(
                method,
                url,
                headers=headers,
                body=body,
                timeout=timeout,
            )
        _note_generation_answered(observed_created_at)
        return payload
    except SessionExpiredError:
        return _rebuild_session_and_repeat(
            session,
            method,
            url,
            headers=headers,
            body=body,
            timeout=timeout,
            budget=budget,
            observed_created_at=observed_created_at,
        )
    except Exception as exc:
        if is_playwright_browser_runtime_error(exc):
            _close_browser_client()
            _raise_browser_runtime_error(exc)
        raise


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


def get_web_session(
    force_refresh: bool = False,
    require_workspace: bool = False,
    account: Optional[str] = None,
) -> WebSession:
    # The lock covers *force_refresh* too. That is the call that logs in, so
    # skipping it was letting every concurrent process past the one gate meant
    # to make them share a single refresh.
    with exclusive_session_refresh(account):
        return _get_web_session(
            force_refresh=force_refresh,
            require_workspace=require_workspace,
            account=account,
        )


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
