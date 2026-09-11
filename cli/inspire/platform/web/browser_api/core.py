"""Shared helpers for browser (web-session) APIs.

The Inspire web UI exposes SSO-only endpoints under ``/api/v2``. Modules such
as inspire.platform.web.browser_api.jobs describe those Actions; this module
centralizes URL resolution, paging guards and compatibility helpers. JSON calls
resolve their owner through inspire.platform.web.runtime and use its Transport.
The thread bridge here is for synchronous browser helpers called from a running
loop, not the execution model of InspireAsyncClient.
"""

from __future__ import annotations

import asyncio
import os
import threading
from contextvars import copy_context
from typing import Any, Optional

from inspire.platform.web.session import (
    WebSession,
    get_playwright_proxy,
)
from inspire.platform.web.session.envelope import (  # noqa: F401 - re-exported
    _is_transient_v2_error_code,
    _v2_result,
)
from inspire.config.models import DEFAULT_BASE_URL
from inspire.platform.web.session.browser_launch import chromium_launch_kwargs

# The platform's JupyterLab entry point, and the one part of `/api/v2` that is
# not an Action: `notebook.GetNotebookLab` / `GetLabUrl` / `GetNotebookProxy` /
# `GetProxyUrl` are all `InvalidAction`. A GET answers `301` to the tokenized
# notebook-gateway URL that actually serves the lab, and the console's own
# JupyterLab iframe points here.
NOTEBOOK_LAB_PATH = "/api/v2/notebook/lab"

# Resolve lazily and key by account/environment; importing must not pin a host.
_cached_base_url: str | None = None
_cached_base_url_key: tuple[str | None, str | None] | None = None


def _active_account_key() -> str | None:
    try:
        from inspire.accounts import current_account

        return current_account()
    except Exception:
        return None


def _base_url_cache_key() -> tuple[str | None, str | None]:
    return (_active_account_key(), os.environ.get("INSPIRE_BASE_URL"))


def clear_browser_api_runtime_cache() -> None:
    """Clear account-sensitive browser API runtime caches."""
    global _cached_base_url, _cached_base_url_key

    _cached_base_url = None
    _cached_base_url_key = None


def _get_base_url() -> str:
    from inspire.platform.web.runtime import get_transport

    return get_transport().base_url


def _configured_base_url() -> str:
    """Resolve CLI configuration independently of an active SDK operation."""
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
    """Apply an init --base-url override to the current CLI configuration key.

    Update an active CLI transport too. Other account/environment keys reload
    their configuration, and an SDK transport keeps its own pinned base URL.
    """
    global _cached_base_url, _cached_base_url_key

    _cached_base_url = url.rstrip("/")
    _cached_base_url_key = _base_url_cache_key()

    from inspire.platform.web.runtime import active_transport

    transport = active_transport.get()
    if transport is not None and transport.cli_compat:
        transport.base_url = _cached_base_url


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

    Negative values are left to the endpoint contract. Some list Actions use
    ``-1`` for every row; inspire.platform.web.browser_api.availability.api
    documents why ListNodeDimension still needs explicit pagination.
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
    from inspire.platform.web.runtime import get_transport

    return get_transport(session).request(
        method, path, body=_clamped_page_size(body), timeout=timeout, referer=referer
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

    thread = threading.Thread(target=copy_context().run, args=(runner,), daemon=True)
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
