"""Proxy helpers for Playwright, requests, and rtunnel operations."""

from __future__ import annotations

import os
from typing import Optional

from inspire.config import Config


def _normalize_proxy(value: object) -> str:
    text = str(value or "").strip()
    return text


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
    return _normalize_proxy(proxies.get("https")) or _normalize_proxy(proxies.get("http"))


def _load_proxy_toml_values() -> tuple[str, dict[str, str]]:
    base_url = _normalize_proxy(os.environ.get("INSPIRE_BASE_URL"))
    values: dict[str, str] = {}
    try:
        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
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
    if system_http or system_https:
        return _build_http_https_pair(system_http, system_https), "system_env"

    return {}, "none"


def resolve_requests_proxy_config() -> tuple[dict[str, str], str]:
    _, toml_values = _load_proxy_toml_values()
    return _resolve_requests_proxy_config_with_toml(toml_values)


def get_playwright_proxy() -> Optional[dict]:
    # Explicit override for browser automation only.
    explicit_proxy = _normalize_proxy(
        os.environ.get("INSPIRE_PLAYWRIGHT_PROXY")
        or os.environ.get("inspire_playwright_proxy")
        or os.environ.get("PLAYWRIGHT_PROXY")
    )
    if explicit_proxy:
        return {"server": explicit_proxy}

    _, toml_values = _load_proxy_toml_values()
    toml_playwright = _normalize_proxy(toml_values.get("playwright"))
    if toml_playwright:
        return {"server": toml_playwright}

    requests_proxies, _ = _resolve_requests_proxy_config_with_toml(toml_values)
    chosen_requests_proxy = _preferred_proxy_server(requests_proxies)

    if chosen_requests_proxy:
        return {"server": chosen_requests_proxy}
    return None


def get_rtunnel_proxy_override() -> str | None:
    explicit = _normalize_proxy(
        os.environ.get("INSPIRE_RTUNNEL_PROXY")
        or os.environ.get("inspire_rtunnel_proxy")
        or os.environ.get("INSPIRE_PLAYWRIGHT_PROXY")
        or os.environ.get("inspire_playwright_proxy")
        or os.environ.get("PLAYWRIGHT_PROXY")
    )
    if explicit:
        return explicit

    _, toml_values = _load_proxy_toml_values()
    toml_rtunnel = _normalize_proxy(toml_values.get("rtunnel"))
    if toml_rtunnel:
        return toml_rtunnel

    requests_proxies, _ = _resolve_requests_proxy_config_with_toml(toml_values)
    chosen_requests_proxy = _preferred_proxy_server(requests_proxies)
    return chosen_requests_proxy or None
