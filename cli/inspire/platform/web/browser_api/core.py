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

from inspire.platform.web.session import (
    TransientAPIError,
    WebSession,
    get_playwright_proxy,
    request_json,
)
from inspire.platform.web.session.browser_launch import chromium_launch_kwargs

DEFAULT_BASE_URL = "https://api.example.com"

# Default browser API prefix (fallback if not configured)
DEFAULT_BROWSER_API_PREFIX = "/api/v1"

# Cached base URL and browser API prefix (loaded once at module import)
_cached_base_url: str | None = None
_cached_base_url_key: tuple[str | None, str | None] | None = None
# Cached browser API prefix (loaded once at module import)
_cached_browser_api_prefix: str | None = None
_cached_browser_api_prefix_key: tuple[str | None, str | None] | None = None


def _active_account_key() -> str | None:
    try:
        from inspire.accounts import current_account

        return current_account()
    except Exception:
        return None


def _base_url_cache_key() -> tuple[str | None, str | None]:
    return (_active_account_key(), os.environ.get("INSPIRE_BASE_URL"))


def _browser_api_prefix_cache_key() -> tuple[str | None, str | None]:
    return (_active_account_key(), os.environ.get("INSPIRE_BROWSER_API_PREFIX"))


def clear_browser_api_runtime_cache() -> None:
    """Clear account-sensitive browser API runtime caches."""
    global _cached_base_url, _cached_base_url_key
    global _cached_browser_api_prefix, _cached_browser_api_prefix_key

    _cached_base_url = None
    _cached_base_url_key = None
    _cached_browser_api_prefix = None
    _cached_browser_api_prefix_key = None


def _get_base_url() -> str:
    """Get base URL from layered config with sane fallback."""
    global _cached_base_url, _cached_base_url_key

    cache_key = _base_url_cache_key()
    if _cached_base_url is not None and _cached_base_url_key == cache_key:
        return _cached_base_url

    try:
        from inspire.config import Config

        config, _ = Config.from_files_and_env(require_credentials=False)
        if config.base_url:
            _cached_base_url = config.base_url
            _cached_base_url_key = cache_key
            return _cached_base_url
    except Exception:
        pass

    _cached_base_url = os.environ.get("INSPIRE_BASE_URL", DEFAULT_BASE_URL)
    _cached_base_url_key = cache_key
    return _cached_base_url


def _set_base_url(url: str) -> None:
    """Override the cached base URL for the current process.

    This is used by plain ``inspire init`` to propagate a CLI-provided
    ``--base-url`` into the module-level cache so that all subsequent
    browser-API calls resolve to the correct host.
    """
    global _cached_base_url, _cached_base_url_key

    _cached_base_url = url.rstrip("/")
    _cached_base_url_key = _base_url_cache_key()


def _get_browser_api_prefix() -> str:
    """Get the browser API prefix from config or environment.

    Returns:
        Browser API prefix (e.g., "/api/v1" or custom)
    """
    global _cached_browser_api_prefix, _cached_browser_api_prefix_key

    cache_key = _browser_api_prefix_cache_key()
    if (
        _cached_browser_api_prefix is not None
        and _cached_browser_api_prefix_key == cache_key
    ):
        return _cached_browser_api_prefix

    # Check environment variable first (highest priority)
    env_prefix = os.environ.get("INSPIRE_BROWSER_API_PREFIX")
    if env_prefix:
        _cached_browser_api_prefix = env_prefix
        _cached_browser_api_prefix_key = cache_key
        return _cached_browser_api_prefix

    # Try to load from config files
    try:
        from inspire.config import Config

        config, _ = Config.from_files_and_env(require_credentials=False)
        if config.browser_api_prefix:
            _cached_browser_api_prefix = config.browser_api_prefix
            _cached_browser_api_prefix_key = cache_key
            return _cached_browser_api_prefix
    except Exception:
        pass

    # Use default
    _cached_browser_api_prefix = DEFAULT_BROWSER_API_PREFIX
    _cached_browser_api_prefix_key = cache_key
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


# v2 carries throttling and server faults in the envelope, under HTTP 200.
# An error announced this way is still the platform declining to answer, and
# must not read as "the answer is empty".
_TRANSIENT_V2_ERROR_CODES = frozenset(
    {
        "internalerror",
        "internalfailure",
        "internalservererror",
        "requesttimeout",
        "serviceunavailable",
        "slowdown",
        "throttling",
        "throttlingexception",
        "toomanyrequests",
        "toomanyrequestsexception",
    }
)


def _is_transient_v2_error_code(code: str) -> bool:
    return str(code or "").strip().lower().replace("_", "") in _TRANSIENT_V2_ERROR_CODES


def _v2_result(data: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the `/api/v2` AWS-style envelope.

    v2 reports business errors inside ``ResponseMetadata.Error`` while the HTTP
    status stays 200, so success can never be inferred from the status code.
    Falls back to the legacy ``code``/``data`` envelope for responses that have
    not moved to v2 yet. Callers pick their own list key out of the result;
    there is no cross-Action convention for it.
    """
    metadata = data.get("ResponseMetadata")
    if isinstance(metadata, dict):
        error = metadata.get("Error")
        if isinstance(error, dict):
            code = error.get("Code") or "Error"
            message = error.get("Message") or "unknown error"
            text = f"API error: {code}: {message}"
            if _is_transient_v2_error_code(code):
                raise TransientAPIError(text)
            raise ValueError(text)
    elif data.get("code") not in (None, 0):
        raise ValueError(f"API error: {data.get('message')}")

    payload = data.get("Result")
    if isinstance(payload, dict):
        return payload
    if payload is None:
        nested_payload = data.get("data")
        if isinstance(nested_payload, dict):
            return nested_payload
    return {}


# The gateway rejects `page_size` above this with
# `InvalidParameter: page or page_size too large`. It is per-service — `hpc`
# enforces it, `ray` currently does not — so callers cap unconditionally
# rather than learning it from a failure.
MAX_PAGE_SIZE = 5000


def _coerce_total(value: Any, fallback: int) -> int:
    """Read a paging `total` that may arrive as an int or a string.

    v2 is inconsistent about this per Action: `notebook.ListNotebooks` answers
    with an int while `hpc.ListJobs` answers with `"202"`. An isinstance check
    against int therefore silently swaps the real total for whatever fallback
    the caller passed, which reads as "this page was the whole list".
    """
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return fallback


def _clamped_page_size(body: Optional[dict]) -> Optional[dict]:
    """Hold `page_size` at the gateway ceiling.

    Above :data:`MAX_PAGE_SIZE` the gateway answers `InvalidParameter: page or
    page_size too large`, and it enforces that per service — `hpc` rejects
    10000 while `ray` accepts it today. Clamping here rather than in each
    wrapper means no caller has to learn the ceiling from a failure, and it
    costs nothing: a request above the ceiling could never have returned more
    rows than one at it.

    ``-1`` means "every row" and the gateway honours it, so it is left alone.
    """
    if not isinstance(body, dict):
        return body
    requested = body.get("page_size")
    if not isinstance(requested, int) or isinstance(requested, bool):
        return body
    if requested <= MAX_PAGE_SIZE:
        return body
    return {**body, "page_size": MAX_PAGE_SIZE}


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
        body=_clamped_page_size(body),
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


def _launch_browser(p, headless: bool = True, *, account: str | None = None):  # noqa: ANN001
    proxy = get_playwright_proxy(account=account)
    return p.chromium.launch(**chromium_launch_kwargs(headless=headless, proxy=proxy))


def _new_context(browser, *, storage_state=None, account: str | None = None):  # noqa: ANN001
    proxy = get_playwright_proxy(account=account)
    if storage_state is not None:
        return browser.new_context(
            storage_state=storage_state, proxy=proxy, ignore_https_errors=True
        )
    return browser.new_context(proxy=proxy, ignore_https_errors=True)
