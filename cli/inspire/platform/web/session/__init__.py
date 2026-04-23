"""Web session management for web UI APIs."""

from __future__ import annotations

import atexit
import logging
from pathlib import Path
from typing import Callable, Optional

import requests as requests_lib

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
    DEFAULT_WORKSPACE_ID,
    SESSION_TTL,
    SessionExpiredError,
    WebSession,
)
from inspire.platform.web.session.proxy import get_playwright_proxy
from inspire.platform.web.session.requests import build_requests_session
from inspire.platform.web.session.workspace import (
    GPUAvailability,
    fetch_gpu_availability as _fetch_gpu_availability,
    fetch_node_specs as _fetch_node_specs,
    fetch_workspace_availability as _fetch_workspace_availability,
)

__all__ = [
    "DEFAULT_WORKSPACE_ID",
    "GPUAvailability",
    "SESSION_TTL",
    "SessionExpiredError",
    "WebSession",
    "build_requests_session",
    "clear_session_cache",
    "fetch_gpu_availability",
    "fetch_node_specs",
    "fetch_workspace_availability",
    "get_credentials",
    "get_playwright_proxy",
    "get_web_session",
    "login_with_playwright",
    "request_json",
]


_BROWSER_API_FORCE_BROWSER = False
logger = logging.getLogger(__name__)


atexit.register(_close_browser_client)


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
    current.created_at = refreshed.created_at


def request_json(
    session: "WebSession",
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    body: Optional[dict] = None,
    timeout: int = 30,
    _retry_count: int = 0,
) -> dict:
    global _BROWSER_API_FORCE_BROWSER

    if not _BROWSER_API_FORCE_BROWSER:
        http = build_requests_session(session, url)
        try:
            method_upper = method.upper()
            req_headers = headers or {}
            if method_upper == "GET":
                resp = http.get(url, headers=req_headers, timeout=timeout)
            elif method_upper == "POST":
                req_headers = dict(req_headers)
                req_headers["Content-Type"] = "application/json"
                resp = http.post(url, headers=req_headers, json=body or {}, timeout=timeout)
            elif method_upper == "DELETE":
                resp = http.delete(url, headers=req_headers, timeout=timeout)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            if resp.status_code == 401:
                raise SessionExpiredError("Session expired or invalid")
            if resp.status_code >= 400:
                raise ValueError(f"API returned {resp.status_code}: {resp.text}")
            try:
                return resp.json()
            except ValueError as e:
                raise SessionExpiredError("Session expired or invalid (non-JSON response)") from e
        except (SessionExpiredError, requests_lib.exceptions.RequestException):
            _BROWSER_API_FORCE_BROWSER = True
        finally:
            http.close()

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
            return _run_in_thread(_browser_request_in_thread)
        client = _get_browser_client(session)
        return client.request_json(
            method,
            url,
            headers=headers,
            body=body,
            timeout=timeout,
        )
    except SessionExpiredError:
        _close_browser_client()
        # Auto-retry once with fresh session
        if _retry_count < 1:
            logger.debug("Web session expired; refreshing cached session.")
            clear_session_cache()
            new_session = get_web_session(force_refresh=True)
            _refresh_session_in_place(session, new_session)
            return request_json(
                session,
                method,
                url,
                headers=headers,
                body=body,
                timeout=timeout,
                _retry_count=_retry_count + 1,
            )
        raise


def get_credentials() -> tuple[str, str]:
    return _get_credentials()


def login_with_playwright(
    username: str,
    password: str,
    base_url: str = "https://api.example.com",
    headless: bool = True,
) -> WebSession:
    return _login_with_playwright(
        username,
        password,
        base_url=base_url,
        headless=headless,
    )


def get_web_session(force_refresh: bool = False, require_workspace: bool = False) -> WebSession:
    return _get_web_session(force_refresh=force_refresh, require_workspace=require_workspace)


def fetch_node_specs(
    session: WebSession,
    compute_group_id: str,
    base_url: str = "https://api.example.com",
) -> dict:
    return _fetch_node_specs(
        session,
        compute_group_id,
        request_json_fn=request_json,
        base_url=base_url,
    )


def fetch_workspace_availability(
    session: WebSession,
    base_url: str = "https://api.example.com",
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> list[dict]:
    return _fetch_workspace_availability(
        session,
        request_json_fn=request_json,
        base_url=base_url,
        progress_callback=progress_callback,
    )


def fetch_gpu_availability(
    session: WebSession,
    compute_group_ids: list[str],
    base_url: str = "https://api.example.com",
) -> list[GPUAvailability]:
    return _fetch_gpu_availability(
        session,
        compute_group_ids,
        request_json_fn=request_json,
        base_url=base_url,
    )


def clear_session_cache() -> None:
    """Remove every ``~/.inspire/accounts/*/web_session.json``."""
    accounts_root = Path.home() / ".inspire" / "accounts"
    if not accounts_root.exists():
        return
    for account_dir in accounts_root.iterdir():
        if not account_dir.is_dir():
            continue
        session_file = account_dir / "web_session.json"
        if session_file.exists():
            try:
                session_file.unlink()
            except Exception:
                continue
