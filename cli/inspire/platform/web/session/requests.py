"""Requests-based session helpers for web UI APIs."""

from __future__ import annotations

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

    if not storage_cookies and session.cookies:
        for name, value in session.cookies.items():
            if not name:
                continue
            jar.set(name, value, domain=base_host, path="/")

    return jar


def build_requests_session(session: WebSession, base_url: str) -> requests.Session:
    storage_cookies = session.storage_state.get("cookies") if session.storage_state else None
    if not storage_cookies and not session.cookies:
        raise ValueError("Session expired or invalid (missing storage state)")

    http = requests.Session()
    http.cookies.update(_cookie_jar_from_session(session, base_url))
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
    if source in {"explicit_env", "toml"} and proxies:
        http.proxies.update(proxies)
        # For explicit Inspire proxy settings, avoid unexpected system-level
        # proxy overrides/no_proxy interactions.
        http.trust_env = False
    return http
