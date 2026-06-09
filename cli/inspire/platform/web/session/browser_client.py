"""Playwright-based request client used as a fallback when cookies expire."""

from __future__ import annotations

import hashlib
import json
import threading
from typing import TYPE_CHECKING, Optional, cast
from weakref import WeakSet

from .models import SessionExpiredError, WebSession
from .browser_launch import chromium_launch_kwargs
from .proxy import get_playwright_proxy

if TYPE_CHECKING:
    from playwright.sync_api import ProxySettings, StorageState


class _BrowserRequestClient:
    def __init__(self, session: WebSession) -> None:
        from playwright.sync_api import sync_playwright

        proxy = cast("ProxySettings | None", get_playwright_proxy(account=session.account))
        self._closed = False
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            **chromium_launch_kwargs(headless=True, proxy=proxy)
        )
        self._context = self._browser.new_context(
            storage_state=cast("StorageState", session.storage_state),
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

        req_headers = headers or {}
        method_upper = method.upper()
        timeout_ms = timeout * 1000

        if method_upper == "GET":
            resp = self._context.request.get(url, headers=req_headers, timeout=timeout_ms)
        elif method_upper == "POST":
            post_headers = dict(req_headers)
            if not any(key.lower() == "content-type" for key in post_headers):
                post_headers["Content-Type"] = "application/json"
            resp = self._context.request.post(
                url,
                headers=post_headers,
                data=json.dumps(body or {}),
                timeout=timeout_ms,
            )
        elif method_upper == "DELETE":
            resp = self._context.request.delete(url, headers=req_headers, timeout=timeout_ms)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        if resp.status == 401:
            raise SessionExpiredError("Session expired or invalid")
        if resp.status >= 400:
            try:
                body_text = resp.text()
            except Exception:
                body_text = ""
            raise ValueError(f"API returned {resp.status}: {body_text}")

        return resp.json()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        try:
            self._context.close()
        except Exception:
            pass
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._playwright.stop()
        except Exception:
            pass


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
        try:
            delattr(_BROWSER_CLIENT_TLS, "client")
        except Exception:
            pass
        return None
    return client


def _set_thread_client(client: _BrowserRequestClient) -> None:
    setattr(_BROWSER_CLIENT_TLS, "client", client)


def _clear_thread_client() -> None:
    try:
        delattr(_BROWSER_CLIENT_TLS, "client")
    except Exception:
        pass


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
        except BaseException:
            pass
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


def _close_browser_client() -> None:
    with _BROWSER_CLIENTS_LOCK:
        clients = list(_BROWSER_CLIENTS)
        _BROWSER_CLIENTS.clear()

    for client in clients:
        _close_client_best_effort(client)

    _clear_thread_client()
