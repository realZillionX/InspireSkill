"""Playwright-based notebook automation (exec + Jupyter navigation)."""

from __future__ import annotations

import json
import os
import shlex
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from inspire.platform.web.browser_api.core import (
    _browser_api_path,
    _get_base_url,
    _in_asyncio_loop,
    _launch_browser,
    _new_context,
    _run_in_thread,
)
from inspire.platform.web.session import WebSession, build_requests_session, get_web_session

COMMAND_COMPLETION_MARKER_PREFIX = "INSPIRE_NOTEBOOK_COMMAND_DONE_"


# ---------------------------------------------------------------------------
# Jupyter navigation
# ---------------------------------------------------------------------------


def _is_lab_like_url(url: str, *, notebook_lab_pattern: str) -> bool:
    value = str(url or "")
    if not value:
        return False

    normalized = value.rstrip("/")
    if "notebook-inspire" in value and normalized.endswith("/lab"):
        return True
    if notebook_lab_pattern.lstrip("/") in value:
        return True
    if "/jupyter/" in value and normalized.endswith("/lab"):
        return True
    return False


def _find_lab_handle(page, *, notebook_lab_pattern: str):  # noqa: ANN001
    for fr in page.frames:
        if _is_lab_like_url(fr.url or "", notebook_lab_pattern=notebook_lab_pattern):
            return fr

    page_url = getattr(page, "url", "") or ""
    if _is_lab_like_url(page_url, notebook_lab_pattern=notebook_lab_pattern):
        return page

    return None


def _wait_for_lab_handle(
    page,  # noqa: ANN001
    *,
    notebook_lab_pattern: str,
    timeout_s: float,
):
    start = time.time()
    while time.time() - start < timeout_s:
        handle = _find_lab_handle(page, notebook_lab_pattern=notebook_lab_pattern)
        if handle is not None:
            return handle
        page.wait_for_timeout(500)
    return None


def open_notebook_lab(page, *, notebook_id: str, timeout: int = 60000):  # noqa: ANN001
    """Open the notebook's JupyterLab and return the lab frame/page handle."""
    base_url = _get_base_url()
    timeout_ms = max(int(timeout), 10000)
    timeout_s = max(timeout_ms // 1000, 10)
    page.goto(
        f"{base_url}/ide?notebook_id={notebook_id}",
        timeout=timeout_ms,
        wait_until="domcontentloaded",
    )

    notebook_lab_pattern = _browser_api_path("/notebook/lab/")
    frame_probe_s = min(10.0, max(4.0, timeout_s / 6.0))
    lab_handle = _wait_for_lab_handle(
        page,
        notebook_lab_pattern=notebook_lab_pattern,
        timeout_s=frame_probe_s,
    )
    if lab_handle is not None:
        return lab_handle

    notebook_lab_prefix = _browser_api_path("/notebook/lab").rstrip("/")
    direct_lab_url = f"{base_url}{notebook_lab_prefix}/{notebook_id}/"
    elapsed_ms = int(frame_probe_s * 1000)
    remaining_ms = max(10000, timeout_ms - elapsed_ms)
    direct_timeout_ms = min(remaining_ms, 20000)
    page.goto(
        direct_lab_url,
        timeout=direct_timeout_ms,
        wait_until="domcontentloaded",
    )
    lab_handle = _wait_for_lab_handle(
        page,
        notebook_lab_pattern=notebook_lab_pattern,
        timeout_s=min(5.0, max(1.0, remaining_ms / 1000.0)),
    )
    if lab_handle is not None:
        return lab_handle

    return page


def build_jupyter_proxy_url(lab_url: str, *, port: int) -> str:
    """Build a Jupyter proxy URL for the given lab URL and port."""
    parsed = urlsplit(lab_url)
    query_token = parse_qs(parsed.query).get("token", [None])[0]

    notebook_lab_pattern = _browser_api_path("/notebook/lab/")
    if notebook_lab_pattern.lstrip("/") in lab_url:
        base_path = parsed.path
        if not base_path.endswith("/"):
            base_path = base_path + "/"
        base_url = urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))
        proxy_url = f"{base_url}proxy/{port}/"
        if query_token:
            return f"{proxy_url}?{urlencode({'token': query_token})}"
        return proxy_url

    path_parts = [part for part in parsed.path.split("/") if part]
    path_token = None
    try:
        jupyter_index = path_parts.index("jupyter")
        if len(path_parts) > jupyter_index + 2:
            path_token = path_parts[jupyter_index + 2]
    except ValueError:
        path_token = None

    base_path = parsed.path.rstrip("/")
    if base_path.endswith("/lab"):
        base_path = base_path[:-4]
    proxy_path = f"{base_path}/proxy/{port}/"

    token = query_token or path_token
    query = urlencode({"token": token}) if token else ""
    return urlunsplit((parsed.scheme, parsed.netloc, proxy_path, query, ""))


# ---------------------------------------------------------------------------
# VSCode proxy suffix — URL normalization
# ---------------------------------------------------------------------------


def _split_ide_gateway(url: str) -> Optional[tuple[str, str, str, str]]:
    """Normalize an IDE gateway / proxy URL into ``(scheme, netloc, path, query)``.

    The IDE iframe settles on a notebook-gateway URL shaped like::

        https://<gateway>/ws-<id>/project-<id>/user-<id>/<ide>/<runtime>/<token>/lab?folder=...

    (a cached rtunnel proxy URL is the same with a trailing ``/proxy/<port>/``).
    Keep the path from ``/ws-`` through ``<token>`` and force the IDE segment —
    the one right after ``user-<id>`` — to ``vscode``; the platform serves the
    same ``<runtime>`` and ``<token>`` under either IDE, so this is a pure marker
    rewrite that drops any ``/lab`` / ``/proxy/...`` tail. When the token rides
    as a ``?token=`` query parameter instead of a path segment, keep that single
    parameter. Returns ``None`` if the URL is not a notebook IDE gateway URL.
    """
    value = str(url or "").strip()
    if not value:
        return None

    parts = urlsplit(value)
    segments = parts.path.split("/")  # keep the leading "" so join restores the leading /

    user_idx = next(
        (idx for idx, seg in enumerate(segments) if seg.startswith("user-")),
        None,
    )
    if user_idx is None:
        return None

    marker_idx = user_idx + 1
    runtime_idx = marker_idx + 1
    token_idx = marker_idx + 2
    if runtime_idx >= len(segments) or not segments[runtime_idx]:
        return None

    has_path_token = token_idx < len(segments) and bool(segments[token_idx])
    end_idx = token_idx if has_path_token else runtime_idx
    kept = segments[:marker_idx] + ["vscode"] + segments[marker_idx + 1 : end_idx + 1]
    path = "/".join(kept)

    query = ""
    if not has_path_token:
        query_token = parse_qs(parts.query).get("token", [None])[0]
        if query_token:
            query = urlencode({"token": query_token})

    return parts.scheme, parts.netloc, path, query


def _vscode_proxy_suffix(url: str) -> Optional[str]:
    """Host-less VSCode proxy suffix (path starting with ``/``, no scheme/host)."""
    parsed = _split_ide_gateway(url)
    if parsed is None:
        return None
    _scheme, _netloc, path, query = parsed
    return urlunsplit(("", "", path, query, ""))


def _ide_gateway_url(url: str) -> Optional[str]:
    """Full VSCode IDE gateway URL (scheme + host), used for caching and probing."""
    parsed = _split_ide_gateway(url)
    if parsed is None:
        return None
    scheme, netloc, path, query = parsed
    if not netloc:
        return None
    return urlunsplit((scheme, netloc, path, query, ""))


def _find_ide_gateway_url(page: Any) -> Optional[str]:
    """Scan a page's frames for the first IDE gateway URL (with host)."""
    for fr in page.frames:
        ide_url = _ide_gateway_url(getattr(fr, "url", "") or "")
        if ide_url:
            return ide_url
    return _ide_gateway_url(getattr(page, "url", "") or "")


# ---------------------------------------------------------------------------
# VSCode proxy suffix — per-account cache, probe, resolution
# ---------------------------------------------------------------------------

_IDE_URL_CACHE_BASENAME = "notebook-ide-url"
_IDE_URL_CACHE_VERSION = 1
DEFAULT_IDE_URL_CACHE_TTL_SECONDS = 8 * 60 * 60


def _active_account_name() -> Optional[str]:
    try:
        from inspire.accounts import current_account

        return current_account()
    except Exception:
        return None


def _ide_url_cache_file(account: Optional[str]) -> Path:
    if account:
        try:
            from inspire.accounts import account_dir, account_exists

            if account_exists(account):
                return account_dir(account) / f"{_IDE_URL_CACHE_BASENAME}.json"
        except Exception:
            pass
    return Path.home() / ".cache" / "inspire-skill" / f"{_IDE_URL_CACHE_BASENAME}.json"


def _load_ide_url_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": _IDE_URL_CACHE_VERSION, "notebooks": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"version": _IDE_URL_CACHE_VERSION, "notebooks": {}}
    notebooks = raw.get("notebooks") if isinstance(raw, dict) else None
    if not isinstance(notebooks, dict):
        notebooks = {}
    return {"version": _IDE_URL_CACHE_VERSION, "notebooks": notebooks}


def _save_ide_url_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        return


def _read_cached_ide_url(
    notebook_id: str,
    base_url: str,
    account: Optional[str],
    ttl_seconds: int,
    *,
    now_ts: Optional[float] = None,
) -> Optional[str]:
    entry = _load_ide_url_cache(_ide_url_cache_file(account)).get("notebooks", {}).get(notebook_id)
    if not isinstance(entry, dict):
        return None
    ide_url = str(entry.get("ide_url") or "").strip()
    if not ide_url:
        return None
    entry_base = str(entry.get("base_url") or "").rstrip("/")
    if entry_base and entry_base != base_url.rstrip("/"):
        return None
    updated_at = float(entry.get("updated_at") or 0)
    now = now_ts if now_ts is not None else time.time()
    if ttl_seconds > 0 and updated_at > 0 and (now - updated_at) > ttl_seconds:
        return None
    return ide_url


def _write_cached_ide_url(
    notebook_id: str,
    ide_url: str,
    base_url: str,
    account: Optional[str],
    *,
    now_ts: Optional[float] = None,
) -> None:
    path = _ide_url_cache_file(account)
    payload = _load_ide_url_cache(path)
    notebooks = payload.setdefault("notebooks", {})
    if not isinstance(notebooks, dict):
        notebooks = {}
        payload["notebooks"] = notebooks
    notebooks[notebook_id] = {
        "ide_url": ide_url,
        "base_url": base_url.rstrip("/"),
        "updated_at": float(now_ts if now_ts is not None else time.time()),
    }
    payload["version"] = _IDE_URL_CACHE_VERSION
    _save_ide_url_cache(path, payload)


def _warm_ide_url_candidates(notebook_id: str, account: Optional[str]) -> list[str]:
    """IDE gateway URLs already cached by ``notebook ssh``/``exec`` for this notebook.

    The rtunnel proxy-state cache and SSH bridges store the full proxy URL —
    which carries the same per-container token — so reuse them as warm probe
    candidates before falling back to the browser.
    """
    candidates: list[str] = []
    try:
        from inspire.platform.web.browser_api.rtunnel import (
            _load_state_file,
            get_rtunnel_state_file,
        )

        entry = (
            _load_state_file(get_rtunnel_state_file(account=account))
            .get("notebooks", {})
            .get(notebook_id)
        )
        if isinstance(entry, dict):
            ide_url = _ide_gateway_url(str(entry.get("proxy_url") or ""))
            if ide_url:
                candidates.append(ide_url)
    except Exception:
        pass
    try:
        from inspire.bridge.tunnel import load_tunnel_config

        config = load_tunnel_config(account=account)
        for bridge in config.bridges.values():
            ide_url = _ide_gateway_url(str(getattr(bridge, "proxy_url", "") or ""))
            if ide_url and notebook_id in ide_url:
                candidates.append(ide_url)
    except Exception:
        pass
    return candidates


def _is_ide_url_live(session: WebSession, ide_url: str, *, timeout_s: float = 8.0) -> bool:
    """Whether *ide_url* is still backed by a live container.

    A live IDE gateway URL redirects (``302`` to ``./?folder=...``); once the
    container restarts and rotates the token, the same URL ``404``s. So treat
    any non-error status (``2xx``/``3xx``) as live.
    """
    http = None
    try:
        http = build_requests_session(session, ide_url)
        resp = http.get(ide_url, timeout=timeout_s, allow_redirects=False)
        return 200 <= resp.status_code < 400
    except Exception:
        return False
    finally:
        if http is not None:
            try:
                http.close()
            except Exception:
                pass


def resolve_notebook_ide_url(
    notebook_id: str,
    *,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 60,
) -> Optional[str]:
    """Drive a headless browser to the notebook IDE and read back its gateway URL.

    Navigates to ``{base}/ide?notebook_id=<id>`` (the entrance the web UI uses)
    and returns the full ``https://<gateway>/ws-.../vscode/<runtime>/<token>``
    URL once the IDE iframe settles, or ``None`` if it never loads (notebook not
    RUNNING).
    """
    if _in_asyncio_loop():
        return _run_in_thread(
            _resolve_notebook_ide_url_sync,
            notebook_id,
            session=session,
            headless=headless,
            timeout=timeout,
        )
    return _resolve_notebook_ide_url_sync(
        notebook_id,
        session=session,
        headless=headless,
        timeout=timeout,
    )


def _resolve_notebook_ide_url_sync(
    notebook_id: str,
    *,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 60,
) -> Optional[str]:
    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    base_url = _get_base_url()
    timeout_ms = max(int(timeout) * 1000, 10000)
    deadline = time.time() + max(int(timeout), 10)

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        context = _new_context(browser, storage_state=session.storage_state)
        page = context.new_page()
        try:
            page.goto(
                f"{base_url}/ide?notebook_id={notebook_id}",
                timeout=timeout_ms,
                wait_until="domcontentloaded",
            )
            while time.time() < deadline:
                ide_url = _find_ide_gateway_url(page)
                if ide_url:
                    return ide_url
                page.wait_for_timeout(500)
            return None
        finally:
            try:
                context.close()
            finally:
                browser.close()


def resolve_notebook_vscode_proxy_suffix(
    notebook_id: str,
    *,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 60,
    refresh: bool = False,
    cache_ttl_seconds: int = DEFAULT_IDE_URL_CACHE_TTL_SECONDS,
) -> Optional[str]:
    """Resolve a notebook's host-less VSCode proxy suffix, caching the live URL.

    The suffix embeds a per-container token that the notebook detail API does
    not expose, so it is read by loading the IDE in a browser. That token is
    stable for the life of the container, so the resolved gateway URL is cached
    per account (keyed by notebook id) and validated with a cheap HTTP probe
    before reuse — the browser only runs on a cold cache or after the token
    rotated (container restart). Cached rtunnel proxy state and SSH bridges,
    which already hold the same token after ``notebook ssh``/``exec``, are reused
    as warm candidates.

    Pass ``refresh=True`` to skip the cache/probe and force a fresh browser
    derivation. Returns ``None`` if the IDE never loads (notebook not RUNNING).
    """
    if session is None:
        session = get_web_session()
    base_url = _get_base_url()
    account = _active_account_name()

    if not refresh:
        cached = _read_cached_ide_url(notebook_id, base_url, account, cache_ttl_seconds)
        candidates = ([cached] if cached else []) + _warm_ide_url_candidates(notebook_id, account)
        seen: set[str] = set()
        for ide_url in candidates:
            if ide_url in seen:
                continue
            seen.add(ide_url)
            if _is_ide_url_live(session, ide_url):
                _write_cached_ide_url(notebook_id, ide_url, base_url, account)
                return _vscode_proxy_suffix(ide_url)

    fresh_url = resolve_notebook_ide_url(
        notebook_id, session=session, headless=headless, timeout=timeout
    )
    if not fresh_url:
        return None
    _write_cached_ide_url(notebook_id, fresh_url, base_url, account)
    return _vscode_proxy_suffix(fresh_url)


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------


def _send_command_via_terminal_ws(
    *,
    context,
    lab_frame,
    command: str,
    timeout_ms: int,
    completion_marker: str | None = None,
) -> bool:
    from inspire.platform.web.browser_api.rtunnel import (
        _build_terminal_websocket_url,
        _create_terminal_via_api,
        _delete_terminal_via_api,
        _send_terminal_command_via_websocket,
    )

    term_name = _create_terminal_via_api(context, lab_frame.url)
    if not term_name:
        return False

    try:
        ws_url = _build_terminal_websocket_url(lab_frame.url, term_name)
        return _send_terminal_command_via_websocket(
            lab_frame,
            ws_url=ws_url,
            command=command,
            timeout_ms=timeout_ms,
            completion_marker=completion_marker,
        )
    finally:
        _delete_terminal_via_api(context, lab_url=lab_frame.url, term_name=term_name)


def _default_completion_marker() -> str:
    return f"{COMMAND_COMPLETION_MARKER_PREFIX}{uuid.uuid4().hex}"


def _wrap_command_for_completion(command: str, completion_marker: str) -> str:
    inner = (
        f"{command}; "
        f"status=$?; "
        f"printf '\\n%s\\n' {shlex.quote(completion_marker)}; "
        "exit $status"
    )
    return f"bash -lc {shlex.quote(inner)}"


def _wait_for_completion_marker(
    lab_frame,  # noqa: ANN001
    *,
    completion_marker: str,
    timeout_ms: int,
) -> bool:
    deadline = time.time() + max(timeout_ms, 1000) / 1000.0
    while time.time() < deadline:
        try:
            found = lab_frame.evaluate(
                """
                marker => {
                  const texts = [];
                  for (const selector of ['.xterm-screen', '.xterm-rows', '.jp-Terminal', 'body']) {
                    for (const node of document.querySelectorAll(selector)) {
                      texts.push(node.innerText || node.textContent || '');
                    }
                  }
                  return texts.join('\\n').includes(marker);
                }
                """,
                completion_marker,
            )
            if found:
                return True
        except Exception:
            pass

        try:
            lab_frame.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)

    return False


def run_command_in_notebook(
    notebook_id: str,
    command: str,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 60,
    completion_marker: str | None = None,
) -> bool:
    """Run a command in a notebook's Jupyter terminal."""
    if _in_asyncio_loop():
        return _run_in_thread(
            _run_command_in_notebook_sync,
            notebook_id=notebook_id,
            command=command,
            session=session,
            headless=headless,
            timeout=timeout,
            completion_marker=completion_marker,
        )
    return _run_command_in_notebook_sync(
        notebook_id=notebook_id,
        command=command,
        session=session,
        headless=headless,
        timeout=timeout,
        completion_marker=completion_marker,
    )


def _run_command_in_notebook_sync(
    notebook_id: str,
    command: str,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 60,
    completion_marker: str | None = None,
) -> bool:
    """Sync implementation for run_command_in_notebook."""
    import sys as _sys

    from playwright.sync_api import sync_playwright

    from inspire.platform.web.browser_api.rtunnel import (
        _focus_terminal_input,
        _open_or_create_terminal,
    )

    if session is None:
        session = get_web_session()

    effective_marker = completion_marker or _default_completion_marker()
    wrapped_command = (
        command if completion_marker else _wrap_command_for_completion(command, effective_marker)
    )

    _sys.stderr.write("Running command in notebook terminal...\n")
    _sys.stderr.flush()

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        context = _new_context(browser, storage_state=session.storage_state)
        page = context.new_page()

        try:
            lab_frame = open_notebook_lab(page, notebook_id=notebook_id)
            timeout_ms = max(int(timeout * 1000), 1000)

            try:
                lab_frame.locator("text=加载中").first.wait_for(state="hidden", timeout=30000)
            except Exception:
                pass

            if _send_command_via_terminal_ws(
                context=context,
                lab_frame=lab_frame,
                command=wrapped_command,
                timeout_ms=timeout_ms,
                completion_marker=effective_marker,
            ):
                return True

            terminal_opened, _term_name = _open_or_create_terminal(context, page, lab_frame)
            if not terminal_opened:
                raise ValueError("Failed to open Jupyter terminal")

            if not _focus_terminal_input(lab_frame, page):
                raise ValueError("Failed to focus Jupyter terminal input")

            page.keyboard.insert_text(wrapped_command)
            page.keyboard.press("Enter")
            return _wait_for_completion_marker(
                lab_frame,
                completion_marker=effective_marker,
                timeout_ms=timeout_ms,
            )

        finally:
            try:
                context.close()
            finally:
                browser.close()


__all__ = [
    "build_jupyter_proxy_url",
    "open_notebook_lab",
    "resolve_notebook_vscode_proxy_suffix",
    "run_command_in_notebook",
]
