"""Shared helpers for browser (web-session) APIs.

The Inspire web UI exposes additional SSO-only endpoints under a configurable prefix.
Domain modules (browser_api_*.py) use this module to avoid copy/pasting URL, prefix,
Playwright, and asyncio-thread bridging logic.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any, Optional

from inspire.platform.web.session import WebSession, get_playwright_proxy, request_json

DEFAULT_BASE_URL = "https://api.example.com"
# Backward-compatible constant (legacy imports). Prefer _get_base_url() for runtime use.
BASE_URL = DEFAULT_BASE_URL

# Default browser API prefix (fallback if not configured)
DEFAULT_BROWSER_API_PREFIX = "/api/v1"

# Cached base URL and browser API prefix (loaded once at module import)
_cached_base_url: str | None = None
# Cached browser API prefix (loaded once at module import)
_cached_browser_api_prefix: str | None = None


def _get_base_url() -> str:
    """Get base URL from layered config with sane fallback."""
    global _cached_base_url

    if _cached_base_url is not None:
        return _cached_base_url

    try:
        from inspire.config import Config

        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
        if config.base_url:
            _cached_base_url = config.base_url
            return _cached_base_url
    except Exception:
        pass

    _cached_base_url = os.environ.get("INSPIRE_BASE_URL", DEFAULT_BASE_URL)
    return _cached_base_url


def _set_base_url(url: str) -> None:
    """Override the cached base URL for the current process.

    This is used by ``init --discover`` to propagate a CLI-provided
    ``--base-url`` into the module-level cache so that all subsequent
    browser-API calls resolve to the correct host.
    """
    global _cached_base_url, BASE_URL

    _cached_base_url = url.rstrip("/")
    BASE_URL = _cached_base_url


def _get_browser_api_prefix() -> str:
    """Get the browser API prefix from config or environment.

    Returns:
        Browser API prefix (e.g., "/api/v1" or custom)
    """
    global _cached_browser_api_prefix

    if _cached_browser_api_prefix is not None:
        return _cached_browser_api_prefix

    # Check environment variable first (highest priority)
    env_prefix = os.environ.get("INSPIRE_BROWSER_API_PREFIX")
    if env_prefix:
        _cached_browser_api_prefix = env_prefix
        return _cached_browser_api_prefix

    # Try to load from config files
    try:
        from inspire.config import Config

        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
        if config.browser_api_prefix:
            _cached_browser_api_prefix = config.browser_api_prefix
            return _cached_browser_api_prefix
    except Exception:
        pass

    # Use default
    _cached_browser_api_prefix = DEFAULT_BROWSER_API_PREFIX
    return _cached_browser_api_prefix


def _browser_api_path(endpoint_path: str) -> str:
    """Build a browser API path with configurable prefix.

    Args:
        endpoint_path: The endpoint path (e.g., "/train_job/list")

    Returns:
        Full path with prefix (e.g., "/api/v1/train_job/list")
    """
    endpoint = endpoint_path.lstrip("/")
    prefix = _get_browser_api_prefix().rstrip("/")
    return f"{prefix}/{endpoint}"


def _request_json(
    session: WebSession,
    method: str,
    path: str,
    *,
    referer: str,
    body: Optional[dict] = None,
    timeout: int = 30,
) -> dict:
    url = f"{_get_base_url()}{path}"
    headers = {"Referer": referer}
    return request_json(
        session,
        method,
        url,
        headers=headers,
        body=body,
        timeout=timeout,
    )


def _in_asyncio_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _run_in_thread(func, *args, **kwargs):  # noqa: ANN001
    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def runner() -> None:
        try:
            result["value"] = func(*args, **kwargs)
        except BaseException as exc:  # pragma: no cover - re-raised in main thread
            error["exc"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error["exc"]
    return result.get("value")


def _launch_browser(p, headless: bool = True):  # noqa: ANN001
    proxy = get_playwright_proxy()
    return p.chromium.launch(headless=headless, proxy=proxy)


def _new_context(browser, *, storage_state=None):  # noqa: ANN001
    proxy = get_playwright_proxy()
    if storage_state is not None:
        return browser.new_context(
            storage_state=storage_state, proxy=proxy, ignore_https_errors=True
        )
    return browser.new_context(proxy=proxy, ignore_https_errors=True)


# Keep BASE_URL in sync for legacy imports.
BASE_URL = _get_base_url()
