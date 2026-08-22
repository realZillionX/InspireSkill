"""Requests-based session helpers for web UI APIs."""

from __future__ import annotations

import atexit
import threading
from urllib.parse import urlsplit

import requests

from .models import WebSession
from .proxy import resolve_requests_proxy_config


def _cookie_jar_from_session(
    session: WebSession, base_url: str
) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    base_host = urlsplit(base_url).hostname or ""

    storage_cookies = session.storage_state.get("cookies") if session.storage_state else None
    if storage_cookies:
        for cookie in storage_cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if not name:
                continue
            domain = cookie.get("domain") or base_host
            path = cookie.get("path") or "/"
            jar.set(name, value, domain=domain, path=path)

    return jar


def _configure(http: requests.Session, session: WebSession, base_url: str) -> requests.Session:
    """Apply this call's cookies, headers, and proxy settings to *http*."""
    # Assigned, not merged: every call starts from the cookies the stored
    # session actually holds, so a jar that outlives one request can never
    # shadow them with something a response set in between.
    http.cookies = _cookie_jar_from_session(session, base_url)
    http.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
    )
    proxies, source = resolve_requests_proxy_config()
    http.proxies.clear()
    http.trust_env = True
    if source in {"explicit_env", "toml"} and proxies:
        http.proxies.update(proxies)
        # For explicit Inspire proxy settings, avoid unexpected system-level
        # proxy overrides/no_proxy interactions.
        http.trust_env = False
    return http


def build_requests_session(session: WebSession, base_url: str) -> requests.Session:
    """Return a private HTTP session the caller owns and may close."""
    storage_cookies = session.storage_state.get("cookies") if session.storage_state else None
    if not storage_cookies:
        raise ValueError("Session expired or invalid (missing storage state)")

    return _configure(requests.Session(), session, base_url)


_pooled_lock = threading.Lock()
_pooled_by_thread: dict[int, requests.Session] = {}


def close_pooled_requests_session() -> None:
    """Drop every thread-local connection pool and close its session."""
    with _pooled_lock:
        stale = list(_pooled_by_thread.values())
        _pooled_by_thread.clear()
    for http in stale:
        try:
            http.close()
        except Exception:  # pragma: no cover - closing must never raise
            pass


atexit.register(close_pooled_requests_session)


def pooled_requests_session(session: WebSession, base_url: str) -> requests.Session:
    """Return this thread's HTTP session, so connections survive the call.

    A workspace-wide question can cost one request per compute group. Building
    a fresh :class:`requests.Session` per request throws the connection away
    each time, so every one of them pays a new TCP connect and TLS handshake --
    measured at ~300 ms against ``qz.sii.edu.cn`` through the local SII proxy,
    against ~30 ms once the connection is reused.

    Requests sessions mutate cookies, headers, and proxy state while preparing
    a request and are not safe to share across the parallel workspace/catalog
    readers. Each thread therefore owns its session while still reusing its
    own urllib3 connections across pages and sibling requests. Cookies,
    headers, and proxy settings are reapplied per call exactly as
    :func:`build_requests_session` does, so a refreshed session never answers
    with a previous one's credentials.

    Callers that close what they are handed must keep using
    :func:`build_requests_session`; closing this one empties the pool for
    everybody.
    """
    storage_cookies = session.storage_state.get("cookies") if session.storage_state else None
    if not storage_cookies:
        raise ValueError("Session expired or invalid (missing storage state)")

    thread_id = threading.get_ident()
    with _pooled_lock:
        http = _pooled_by_thread.get(thread_id)
        if http is None:
            http = requests.Session()
            _pooled_by_thread[thread_id] = http
        return _configure(http, session, base_url)
