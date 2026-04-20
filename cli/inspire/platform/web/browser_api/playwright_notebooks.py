"""Playwright-based notebook automation (exec + Jupyter navigation)."""

from __future__ import annotations

import shlex
import time
import uuid
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from inspire.platform.web.browser_api.core import (
    _browser_api_path,
    _get_base_url,
    _in_asyncio_loop,
    _launch_browser,
    _new_context,
    _run_in_thread,
)
from inspire.platform.web.session import WebSession, get_web_session

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
    "run_command_in_notebook",
]
