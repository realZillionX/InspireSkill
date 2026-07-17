"""Proxy helpers for Playwright, requests, and rtunnel operations."""

from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from inspire.config import Config


def _normalize_proxy(value: object) -> str:
    text = str(value or "").strip()
    return text


def redact_proxy_url(value: object) -> str:
    """Return a diagnostic-safe proxy URL without credentials or URL secrets."""
    text = _normalize_proxy(value)
    if not text:
        return ""

    try:
        parsed = urlsplit(text)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return "<configured>"

    if not parsed.scheme or not host:
        return "<configured>"

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if port is not None:
        netloc = f"{netloc}:{port}"
    if parsed.username or parsed.password:
        netloc = f"<redacted>@{netloc}"
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def describe_proxy_config(proxies: dict[str, str]) -> dict[str, str]:
    """Redact a requests-style proxy mapping for logs and diagnostics."""
    return {key: redact_proxy_url(value) for key, value in sorted(proxies.items())}


def _build_http_https_pair(http_value: str, https_value: str) -> dict[str, str]:
    http_proxy = _normalize_proxy(http_value)
    https_proxy = _normalize_proxy(https_value)
    if not http_proxy and not https_proxy:
        return {}
    return {
        "http": http_proxy or https_proxy,
        "https": https_proxy or http_proxy,
    }


def _preferred_proxy_server(proxies: dict[str, str]) -> str:
    return (
        _normalize_proxy(proxies.get("https"))
        or _normalize_proxy(proxies.get("all"))
        or _normalize_proxy(proxies.get("http"))
    )


def _playwright_bypass_from_no_proxy() -> str:
    raw = _normalize_proxy(os.environ.get("no_proxy") or os.environ.get("NO_PROXY"))
    if not raw:
        return ""

    items: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if item == "*" or item.startswith("*."):
            items.append(item)
        elif item.startswith("."):
            items.append(f"*{item}")
        else:
            items.append(item)
    return ",".join(dict.fromkeys(items))


def _playwright_proxy_dict(server: str) -> dict[str, str]:
    proxy = {"server": server}
    bypass = _playwright_bypass_from_no_proxy()
    if bypass:
        proxy["bypass"] = bypass
    return proxy


def _no_proxy_match(url: str) -> str:
    raw = _normalize_proxy(os.environ.get("no_proxy") or os.environ.get("NO_PROXY"))
    if not raw or not url:
        return "not_set"

    try:
        from requests.utils import should_bypass_proxies

        matched = should_bypass_proxies(url, no_proxy=raw)
    except Exception:
        matched = False
    return "matched" if matched else "not_matched"


def _load_proxy_toml_values(account: str | None = None) -> tuple[str, dict[str, str]]:
    base_url = _normalize_proxy(os.environ.get("INSPIRE_BASE_URL"))
    values: dict[str, str] = {}
    try:
        if account:
            config, _ = Config.from_files_and_env(require_credentials=False, account=account)
        else:
            config, _ = Config.from_files_and_env(require_credentials=False)
    except Exception:
        return base_url, values

    if getattr(config, "base_url", None):
        base_url = _normalize_proxy(config.base_url)

    requests_http = _normalize_proxy(getattr(config, "requests_http_proxy", None))
    requests_https = _normalize_proxy(getattr(config, "requests_https_proxy", None))
    playwright_proxy = _normalize_proxy(getattr(config, "playwright_proxy", None))
    rtunnel_proxy = _normalize_proxy(getattr(config, "rtunnel_proxy", None))

    if requests_http:
        values["requests_http"] = requests_http
    if requests_https:
        values["requests_https"] = requests_https
    if playwright_proxy:
        values["playwright"] = playwright_proxy
    if rtunnel_proxy:
        values["rtunnel"] = rtunnel_proxy

    return base_url, values


def _resolve_requests_proxy_config_with_toml(
    toml_values: dict[str, str],
) -> tuple[dict[str, str], str]:
    explicit_http = _normalize_proxy(os.environ.get("INSPIRE_REQUESTS_HTTP_PROXY"))
    explicit_https = _normalize_proxy(os.environ.get("INSPIRE_REQUESTS_HTTPS_PROXY"))
    if explicit_http or explicit_https:
        return _build_http_https_pair(explicit_http, explicit_https), "explicit_env"

    toml_http = _normalize_proxy(toml_values.get("requests_http"))
    toml_https = _normalize_proxy(toml_values.get("requests_https"))
    if toml_http or toml_https:
        return _build_http_https_pair(toml_http, toml_https), "toml"

    system_http = _normalize_proxy(os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY"))
    system_https = _normalize_proxy(os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY"))
    system_all = _normalize_proxy(os.environ.get("all_proxy") or os.environ.get("ALL_PROXY"))
    if system_http or system_https or system_all:
        # Preserve standard Requests semantics: scheme-specific variables win,
        # while ALL_PROXY is the fallback for other schemes.
        system_proxies: dict[str, str] = {}
        if system_http:
            system_proxies["http"] = system_http
        if system_https:
            system_proxies["https"] = system_https
        if system_all:
            system_proxies["all"] = system_all
        return system_proxies, "system_env"

    return {}, "none"


def resolve_requests_proxy_config(account: str | None = None) -> tuple[dict[str, str], str]:
    _, toml_values = _load_proxy_toml_values(account)
    return _resolve_requests_proxy_config_with_toml(toml_values)


def _resolve_playwright_proxy_config_with_toml(
    toml_values: dict[str, str],
) -> tuple[Optional[dict[str, str]], str]:
    # Explicit override for browser automation only.
    explicit_proxy = _normalize_proxy(
        os.environ.get("INSPIRE_PLAYWRIGHT_PROXY")
        or os.environ.get("inspire_playwright_proxy")
        or os.environ.get("PLAYWRIGHT_PROXY")
    )
    if explicit_proxy:
        return _playwright_proxy_dict(explicit_proxy), "explicit_env"

    toml_playwright = _normalize_proxy(toml_values.get("playwright"))
    if toml_playwright:
        return _playwright_proxy_dict(toml_playwright), "toml"

    requests_proxies, requests_source = _resolve_requests_proxy_config_with_toml(toml_values)
    chosen_requests_proxy = _preferred_proxy_server(requests_proxies)

    if chosen_requests_proxy:
        return _playwright_proxy_dict(chosen_requests_proxy), f"requests:{requests_source}"
    return None, "none"


def resolve_playwright_proxy_config(
    account: str | None = None,
) -> tuple[Optional[dict[str, str]], str]:
    _, toml_values = _load_proxy_toml_values(account)
    return _resolve_playwright_proxy_config_with_toml(toml_values)


def get_playwright_proxy(account: str | None = None) -> Optional[dict]:
    proxy, _ = resolve_playwright_proxy_config(account)
    return proxy


def _resolve_rtunnel_proxy_config_with_toml(
    toml_values: dict[str, str],
) -> tuple[str | None, str]:
    explicit = _normalize_proxy(
        os.environ.get("INSPIRE_RTUNNEL_PROXY")
        or os.environ.get("inspire_rtunnel_proxy")
        or os.environ.get("INSPIRE_PLAYWRIGHT_PROXY")
        or os.environ.get("inspire_playwright_proxy")
        or os.environ.get("PLAYWRIGHT_PROXY")
    )
    if explicit:
        return explicit, "explicit_env"

    toml_rtunnel = _normalize_proxy(toml_values.get("rtunnel"))
    if toml_rtunnel:
        return toml_rtunnel, "toml"

    requests_proxies, requests_source = _resolve_requests_proxy_config_with_toml(toml_values)
    chosen_requests_proxy = _preferred_proxy_server(requests_proxies)
    if chosen_requests_proxy:
        return chosen_requests_proxy, f"requests:{requests_source}"
    return None, "none"


def resolve_rtunnel_proxy_config(account: str | None = None) -> tuple[str | None, str]:
    _, toml_values = _load_proxy_toml_values(account)
    return _resolve_rtunnel_proxy_config_with_toml(toml_values)


def get_rtunnel_proxy_override(account: str | None = None) -> str | None:
    proxy, _ = resolve_rtunnel_proxy_config(account)
    return proxy


def describe_effective_proxy_config(
    account: str | None = None,
    *,
    base_url: str | None = None,
) -> dict[str, object]:
    """Describe effective HTTP(S) proxy routing without exposing proxy secrets."""
    configured_base_url, toml_values = _load_proxy_toml_values(account)
    target_url = _normalize_proxy(base_url) or configured_base_url
    target_host = ""
    target_scheme = ""
    try:
        parsed_target = urlsplit(target_url)
        target_host = parsed_target.hostname or ""
        target_scheme = parsed_target.scheme.lower()
    except ValueError:
        target_host = ""
        target_scheme = ""

    requests_proxies, requests_source = _resolve_requests_proxy_config_with_toml(toml_values)
    requests_no_proxy = (
        _no_proxy_match(target_url) if requests_source == "system_env" else "not_applicable"
    )
    try:
        from requests.utils import select_proxy

        requests_target_proxy = select_proxy(target_url, requests_proxies)
    except Exception:
        requests_target_proxy = requests_proxies.get(target_scheme) or requests_proxies.get("all")
    requests_route = "proxy" if requests_target_proxy else "direct"
    if requests_no_proxy == "matched":
        requests_route = "direct"

    playwright_proxy, playwright_source = _resolve_playwright_proxy_config_with_toml(toml_values)
    playwright_no_proxy = (
        _no_proxy_match(target_url) if playwright_proxy is not None else "not_applicable"
    )
    playwright_route = "proxy" if playwright_proxy is not None else "direct"
    if playwright_no_proxy == "matched":
        playwright_route = "direct"

    rtunnel_proxy, rtunnel_source = _resolve_rtunnel_proxy_config_with_toml(toml_values)

    return {
        "target": target_host or None,
        "requests": {
            "source": requests_source,
            "route": requests_route,
            "http": redact_proxy_url(requests_proxies.get("http")) or None,
            "https": redact_proxy_url(requests_proxies.get("https")) or None,
            "all": redact_proxy_url(requests_proxies.get("all")) or None,
            "selected": redact_proxy_url(requests_target_proxy) or None,
            "no_proxy": requests_no_proxy,
        },
        "playwright": {
            "source": playwright_source,
            "route": playwright_route,
            "server": (
                redact_proxy_url(playwright_proxy.get("server"))
                if playwright_proxy is not None
                else None
            ),
            "no_proxy": playwright_no_proxy,
        },
        "rtunnel": {
            "source": rtunnel_source,
            "route": "proxy" if rtunnel_proxy else "direct",
            "server": redact_proxy_url(rtunnel_proxy) or None,
        },
    }
