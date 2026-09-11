"""Playwright-based request client used as a fallback when cookies expire."""

from __future__ import annotations

from inspire.platform.web.offload import offload

from inspire.platform.web.flow import blocking_io

import contextlib
import logging
import hashlib
import json
import threading
from typing import Any, Optional, cast
from weakref import WeakSet

from .models import (
    TRANSIENT_HTTP_STATUSES,
    SessionExpiredError,
    TransientAPIError,
    WebSession,
)
from .browser_launch import chromium_launch_kwargs
from .proxy import get_playwright_proxy
from .retry import retry_after_seconds


logger = logging.getLogger(__name__)


class _BrowserHTTPError(ValueError):
    def __init__(self, status: int, body: str):
        super().__init__(f"API returned {status}: {body}")
        self.status = status
        self.body = body


class _BrowserRequestClient:
    def __init__(self, session: WebSession) -> None:
        from playwright.sync_api import sync_playwright

        proxy = cast(Any, get_playwright_proxy(account=session.account))
        self._closed = False
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            **chromium_launch_kwargs(headless=True, proxy=proxy)
        )
        self._context = self._browser.new_context(
            storage_state=cast(Any, session.storage_state),
            proxy=proxy,
            ignore_https_errors=True,
        )
        self.session_fingerprint = _session_fingerprint(session)

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        body: Optional[dict] = None,
        timeout: int = 30,
    ) -> dict:
        if self._closed:
            raise RuntimeError("Browser request client is closed")

        method_upper, options = _request_options(method, headers, body, timeout)
        resp = getattr(self._context.request, method_upper.lower())(url, **options)
        try:
            body_text = resp.text() if resp.status >= 400 else ""
        except Exception:
            logger.debug("Browser error response body unavailable; classifying HTTP status", exc_info=True)
            body_text = ""
        _classify_response(
            resp.status, resp.headers if resp.status in TRANSIENT_HTTP_STATUSES else {}, body_text
        )

        return resp.json()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        # The context may already be closed; continue releasing browser resources.
        with contextlib.suppress(Exception):
            self._context.close()
        # The browser may already be closed; still stop the Playwright runtime.
        with contextlib.suppress(Exception):
            self._browser.close()
        # An already stopped Playwright runtime needs no further cleanup.
        with contextlib.suppress(Exception):
            self._playwright.stop()


def _session_fingerprint(session: WebSession) -> str:
    cookies = session.storage_state.get("cookies") if session.storage_state else []
    payload = json.dumps(
        [
            {
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain"),
                "path": c.get("path"),
            }
            for c in cookies or []
        ],
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


_BROWSER_CLIENT_TLS = threading.local()
_BROWSER_CLIENTS: "WeakSet[_BrowserRequestClient]" = WeakSet()
_BROWSER_CLIENTS_LOCK = threading.Lock()
_BROWSER_CLIENT_CLOSE_TIMEOUT_SECONDS = 1.0


def _get_thread_client() -> Optional[_BrowserRequestClient]:
    client = getattr(_BROWSER_CLIENT_TLS, "client", None)
    if client is not None and getattr(client, "_closed", False):
        # A stale thread-local entry must not prevent reporting that no client is available.
        with contextlib.suppress(AttributeError):
            delattr(_BROWSER_CLIENT_TLS, "client")
        return None
    return client


def _set_thread_client(client: _BrowserRequestClient) -> None:
    setattr(_BROWSER_CLIENT_TLS, "client", client)


def _clear_thread_client() -> None:
    # The thread-local client may already be absent during cleanup.
    with contextlib.suppress(AttributeError):
        delattr(_BROWSER_CLIENT_TLS, "client")


def _register_client(client: _BrowserRequestClient) -> None:
    with _BROWSER_CLIENTS_LOCK:
        _BROWSER_CLIENTS.add(client)


def _unregister_client(client: _BrowserRequestClient) -> None:
    with _BROWSER_CLIENTS_LOCK:
        _BROWSER_CLIENTS.discard(client)


def _get_browser_client(session: WebSession) -> _BrowserRequestClient:
    fingerprint = _session_fingerprint(session)
    client = _get_thread_client()
    if client and client.session_fingerprint == fingerprint:
        return client

    if client:
        _close_client_best_effort(client)
        _unregister_client(client)
        _clear_thread_client()

    client = _BrowserRequestClient(session)
    _set_thread_client(client)
    _register_client(client)
    return client


def _close_client_best_effort(client: _BrowserRequestClient, *, timeout: float | None = None) -> bool:
    """Close a Playwright request client without letting cleanup block CLI exit."""
    done = threading.Event()

    def _runner() -> None:
        try:
            client.close()
        except Exception:
            logger.debug("Browser cleanup worker failed; continuing shutdown", exc_info=True)
        finally:
            done.set()

    thread = threading.Thread(
        target=_runner,
        name="inspire-browser-client-close",
        daemon=True,
    )
    thread.start()
    thread.join(
        _BROWSER_CLIENT_CLOSE_TIMEOUT_SECONDS if timeout is None else max(0.0, timeout)
    )
    return done.is_set()


@blocking_io
def _close_browser_client() -> None:
    with _BROWSER_CLIENTS_LOCK:
        clients = list(_BROWSER_CLIENTS)
        _BROWSER_CLIENTS.clear()

    for client in clients:
        _close_client_best_effort(client)

    _clear_thread_client()


def _request_options(method: str, headers: dict[str, str] | None,
                     body: dict | None, timeout: float) -> tuple[str, dict[str, Any]]:
    method_upper = method.upper()
    if method_upper not in {"GET", "POST", "DELETE"}:
        raise ValueError(f"Unsupported HTTP method: {method}")
    req_headers = dict(headers or {})
    options: dict[str, Any] = dict(headers=req_headers, timeout=timeout * 1000, max_redirects=0)
    if method_upper == "POST":
        if not any(key.lower() == "content-type" for key in req_headers):
            req_headers["Content-Type"] = "application/json"
        options["data"] = json.dumps(body or {})
    return method_upper, options


def _classify_response(status: int, headers: dict[str, str], body_text: str) -> None:
    if status == 401 or 300 <= status < 400:
        raise SessionExpiredError("Session expired or invalid")
    if status >= 400:
        message = f"API returned {status}: {body_text}"
        if status in TRANSIENT_HTTP_STATUSES:
            raise TransientAPIError(
                message,
                status=status,
                retry_after=retry_after_seconds(headers),
            )
        raise _BrowserHTTPError(status, body_text)


class AsyncBrowserRequestClient:
    """Disposable Playwright client owned by its creating event loop."""

    def __init__(self, session: WebSession):
        self.session = session
        self._closed = False
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    async def __aenter__(self) -> AsyncBrowserRequestClient:
        from playwright.async_api import async_playwright

        proxy = cast(Any, await offload(get_playwright_proxy, account=self.session.account))
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                **chromium_launch_kwargs(headless=True, proxy=proxy)
            )
            self._context = await self._browser.new_context(
                storage_state=cast(Any, self.session.storage_state), proxy=proxy,
                ignore_https_errors=True,
            )
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def request_json(self, method: str, url: str, *,
                           headers: dict[str, str] | None = None,
                           body: dict | None = None, timeout: float = 30) -> dict:
        if self._closed:
            raise RuntimeError("Browser request client is closed")
        method_upper, options = _request_options(method, headers, body, timeout)
        resp = await getattr(self._context.request, method_upper.lower())(url, **options)
        try:
            body_text = await resp.text() if resp.status >= 400 else ""
        except Exception:
            logger.debug("Browser error response body unavailable; classifying HTTP status", exc_info=True)
            body_text = ""
        _classify_response(
            resp.status, resp.headers if resp.status in TRANSIENT_HTTP_STATUSES else {}, body_text
        )
        return await resp.json()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for resource, method in [(self._context, "close"), (self._browser, "close"),
                                 (self._playwright, "stop")]:
            if resource is not None:
                # An already closed resource must not prevent releasing the rest.
                with contextlib.suppress(Exception):
                    await getattr(resource, method)()
