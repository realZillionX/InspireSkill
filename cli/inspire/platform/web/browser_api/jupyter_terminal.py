from __future__ import annotations

from inspire.services.execution.async_output import deliver_output

from inspire.exec_output import (
    DEFAULT_MAX_OUTPUT_BYTES,
    OutputTarget,
    OutputBuffer,
    TerminalScanner,
    output_writer,
    validate_capture,
)

import asyncio
from inspire.services.execution.async_output import async_output_writer
import base64
import contextlib
import json
import logging
import re
import select
import shlex
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Protocol, Optional
from urllib.parse import urlsplit

from inspire.platform.web import jupyter_urls as rtunnel_module
from inspire.platform.web.browser_api.core import (
    _in_asyncio_loop,
    _run_in_thread,
)
from inspire.platform.web.pty_socket import JobShellAuthError
from inspire.platform.web.session.models import SessionExpiredError
from inspire.platform.web.session import WebSession
from inspire.platform.web.session import get_web_session
from inspire.platform.web.runtime import get_transport

JUPYTER_DONE_PREFIX = "__INSPIRE_JUPYTER_DONE_"
MISSING_MARKER_RETURN_CODE = 124
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# The terminal echoes a shell prompt when it is ready for input.
_PROMPT_RE = re.compile(r"[$#]\s*$")
_EXIT_CODE_RE = re.compile(r"^\d+\s")
# The PTY drops input that arrives faster than it drains, so feed it in chunks.
_STDIN_CHUNK = 2048
_STDIN_CHUNK_DELAY_S = 0.05

logger = logging.getLogger(__name__)


class _TextWebSocket(Protocol):
    def send_text(self, text: str) -> None: ...


@dataclass(frozen=True)
class JupyterCommandResult:
    returncode: int
    output: str
    completed: bool
    marker: str
    truncated: bool = False
    total_output_bytes: int = 0


def new_completion_marker() -> str:
    return f"{JUPYTER_DONE_PREFIX}{uuid.uuid4().hex}"


def build_jupyter_exec_command(command: str, *, marker: str) -> str:
    script = "\n".join(
        [
            "set +e",
            "(",
            command,
            # The outer bash reads this control script from stdin. A user
            # command that inherits the same fd (for example `cat` or
            # `bash -s`) can otherwise consume the status/marker tail before
            # the outer shell parses it. Give the command a closed default
            # stdin; an explicit remote pipe or `< /inspire/...` redirection
            # inside `command` still overrides this subshell redirection.
            ") </dev/null",
            "__inspire_status=$?",
            f"printf '\\n%s:exit:%s\\n' {shlex.quote(marker)} \"$__inspire_status\"",
            "exit \"$__inspire_status\"",
            "",
        ]
    )
    encoded = base64.b64encode(script.encode()).decode("ascii")
    return f"echo '{encoded}' | base64 -d | bash\r"


def _strip_jupyter_terminal_prelude(output: str) -> str:
    lines = output.splitlines(keepends=True)
    command_line_end = 0
    for index, line in enumerate(lines):
        plain = _ANSI_CSI_RE.sub("", line)
        if "echo '" in plain and "| base64 -d | bash" in plain:
            command_line_end = index + 1

    cleaned = "".join(lines[command_line_end:])
    return re.sub(r"^(?:\x1b\[\?2004[lh]\r?|\r)+", "", cleaned)


def parse_jupyter_exec_output(raw_output: str, *, marker: str) -> JupyterCommandResult:
    pattern = re.compile(rf"{re.escape(marker)}:exit:(\d+)\s*")
    match = pattern.search(raw_output)
    if not match:
        return JupyterCommandResult(
            returncode=MISSING_MARKER_RETURN_CODE,
            output=_strip_jupyter_terminal_prelude(raw_output),
            completed=False,
            marker=marker,
        )
    output = _strip_jupyter_terminal_prelude(raw_output[: match.start()])
    return JupyterCommandResult(
        returncode=int(match.group(1)),
        output=output,
        completed=True,
        marker=marker,
    )


class TerminalOutput:
    def __init__(self, marker: str, limit: int | None, capture: bool):
        self.marker, self.limit, self.capture = marker, limit, capture
        self.scanner = TerminalScanner(marker)
        # Extra head space preserves the echoed bootstrap even with a small output cap.
        self.buffer = OutputBuffer(None if limit is None else limit + 131072, capture=capture)

    def feed(self, text: str) -> None:
        self.scanner.feed(text)
        self.buffer.feed(text)

    def result(self) -> JupyterCommandResult:
        parsed = parse_jupyter_exec_output(
            self.buffer.text() if self.capture else self.scanner.tail, marker=self.marker
        )
        output = OutputBuffer(self.limit, capture=self.capture)
        output.feed(parsed.output)
        code = self.scanner.returncode
        return JupyterCommandResult(
            code if code is not None else parsed.returncode,
            output.text(),
            code is not None or parsed.completed,
            self.marker,
            self.buffer.truncated or output.truncated,
            self.buffer.total,
        )


def build_jupyter_terminal_ws_url(lab_url: str, term_name: str) -> str:
    return rtunnel_module.build_terminal_websocket_url(lab_url, term_name)


def build_shell_bootstrap(*, cwd: str | None, env_exports: str) -> str:
    """Build the login-shell bootstrap for an interactive JupyterTerminal.

    The shell runs as a child rather than via ``exec`` so that the surviving
    parent can announce the exit — the gateway keeps the websocket open after
    the shell is gone, so nothing else tells the client to stop reading. See
    ``job_shell.SHELL_EXIT_MARKER``.
    """
    from inspire.platform.web.pty_socket import shell_exit_announce

    tail = f"$SHELL -l; {shell_exit_announce()}\r"
    if cwd:
        return f"{env_exports}cd {shlex.quote(cwd)} && {tail}"
    return f"{env_exports}{tail}"


def run_command_capture_in_notebook(
    *,
    notebook_id: str,
    command: str,
    session: Optional[WebSession] = None,
    timeout: float = 60,
    marker: str | None = None,
    on_output: Callable[[str], None] | None = None,
    max_output_bytes: int | None = DEFAULT_MAX_OUTPUT_BYTES,
    output_to: OutputTarget = None,
    capture: bool = True,
) -> JupyterCommandResult:
    if _in_asyncio_loop():
        return _run_in_thread(
            _run_command_capture_in_notebook_sync,
            notebook_id=notebook_id,
            command=command,
            session=session,
            timeout=timeout,
            marker=marker,
            on_output=on_output,
            max_output_bytes=max_output_bytes,
            output_to=output_to,
            capture=capture,
        )
    return _run_command_capture_in_notebook_sync(
        notebook_id=notebook_id,
        command=command,
        session=session,
        timeout=timeout,
        marker=marker,
        on_output=on_output,
        max_output_bytes=max_output_bytes,
        output_to=output_to,
        capture=capture,
    )


@dataclass
class _JupyterTerminal:
    """One Jupyter terminal, created and torn down over plain HTTP."""

    lab_url: str
    name: str
    ws_url: str


def _notebook_jupyter_url(session: WebSession, notebook_id: str) -> str:
    """The notebook's JupyterLab entrance, straight from the platform.

    Deliberately the raw ``jupyter_url``: the terminal REST and WebSocket
    routes hang off the Jupyter server base, so the ``vscode`` rewrite that
    :func:`playwright_notebooks._ide_gateway_url` applies is wrong here.
    """
    from inspire.platform.web.browser_api.notebooks import _notebook_v2

    payload = _notebook_v2(session, "GetNotebookAccessUrl", {"notebook_id": notebook_id})
    return str(payload.get("jupyter_url") or "").strip()


@contextlib.contextmanager
def _jupyter_terminal(
    session: WebSession,
    notebook_id: str,
    *,
    timeout_s: float = 30.0,
) -> Iterator[Optional[_JupyterTerminal]]:
    """Create a terminal for the notebook, and always clean it up.

    Yields ``None`` when the notebook has no reachable Jupyter server — a
    STOPPED notebook returns an empty access URL — so callers report the same
    "could not start a terminal" outcome they always did.

    No browser anywhere in here. `_xsrf` is a plain cookie the Jupyter server
    sets on any GET, and the REST API only wants it echoed back in a header;
    driving Chromium to obtain it was never necessary.
    """
    lab_url = _notebook_jupyter_url(session, notebook_id)
    if not lab_url:
        logger.debug("JupyterTerminal access URL is empty")
        yield None
        return

    connection = get_transport(session).application_connection(lab_url)
    http = connection.__enter__()
    term_name = ""
    base = rtunnel_module.jupyter_server_base(lab_url)
    try:
        entrance = http.get(lab_url, timeout=(5, timeout_s), allow_redirects=True)
        if entrance.status_code == 401:
            raise SessionExpiredError("Jupyter terminal session expired (401).")
        logger.debug("JupyterTerminal entrance GET status=%s", entrance.status_code)
        xsrf = str(http.cookies.get("_xsrf") or "")
        logger.debug("JupyterTerminal XSRF cookie present=%s", bool(xsrf))
        headers = {"X-XSRFToken": xsrf} if xsrf else {}
        response = http.post(f"{base}api/terminals", headers=headers, timeout=(5, timeout_s))
        logger.debug("JupyterTerminal create POST status=%s", response.status_code)
        if response.status_code == 401:
            raise SessionExpiredError("Jupyter terminal session expired (401).")
        if response.status_code not in (200, 201):
            yield None
            return
        term_name = str(response.json().get("name") or "")
        if not term_name:
            logger.debug("JupyterTerminal create response omitted the terminal name")
            yield None
            return
        logger.debug("JupyterTerminal created; opening WebSocket")
        yield _JupyterTerminal(
            lab_url=lab_url,
            name=term_name,
            ws_url=rtunnel_module.build_terminal_websocket_url(lab_url, term_name),
        )
    finally:
        if term_name:
            with contextlib.suppress(Exception):
                http.delete(
                    f"{base}api/terminals/{term_name}",
                    headers={"X-XSRFToken": str(http.cookies.get("_xsrf") or "")},
                    timeout=(5, timeout_s),
                )
        with contextlib.suppress(Exception):
            connection.__exit__(None, None, None)


def _capture_terminal_output(
    *,
    ws_url: str,
    session: WebSession,
    stdin_data: str,
    timeout_ms: int,
    marker: str,
    on_output: Callable[[str], None] | None = None,
    max_output_bytes: int | None = DEFAULT_MAX_OUTPUT_BYTES,
    output_to: OutputTarget = None,
    capture: bool = True,
) -> Optional[JupyterCommandResult]:
    """Run one command on the terminal and read back everything it printed.

    A Python port of what used to run as JavaScript inside a Playwright page.
    The protocol is unchanged: wait for a prompt (or give up waiting and send
    anyway), feed stdin in chunks, and stop as soon as the marker line carries
    an exit code.
    """
    from inspire.platform.web.pty_socket import WebSocketClient

    deadline = time.monotonic() + max(int(timeout_ms), 1) / 1000.0
    # Bounded wait for the prompt: a terminal that never prints one still has
    # to receive the command, or the call would return empty on a timeout.
    prompt_deadline = time.monotonic() + max(0, min(timeout_ms - 500, 3000)) / 1000.0
    validate_capture(max_output_bytes, capture, output_to)
    output = TerminalOutput(marker, max_output_bytes, capture)
    sent = False
    callback_failed = False

    def _send(ws: _TextWebSocket) -> None:
        for start in range(0, len(stdin_data), _STDIN_CHUNK):
            _send_jupyter_stdin(ws, stdin_data[start : start + _STDIN_CHUNK])
            if start + _STDIN_CHUNK < len(stdin_data):
                time.sleep(_STDIN_CHUNK_DELAY_S)

    with output_writer(output_to) as writer:
        try:
            with WebSocketClient(
                ws_url, _jupyter_ws_headers(session, ws_url), timeout=max(timeout_ms / 1000, 0.001)
            ) as ws:
                while True:
                    now = time.monotonic()
                    if now >= deadline:
                        break
                    if not sent and now >= prompt_deadline:
                        sent = True
                        _send(ws)
                    ready, _, _ = select.select([ws], [], [], min(0.25, deadline - now))
                    if not ready and not ws.has_pending_data():
                        continue
                    ws.set_read_timeout(max(0.001, deadline - time.monotonic()))
                    try:
                        opcode, payload = ws.recv_frame()
                    except EOFError:
                        break
                    if opcode == 0x8:
                        break
                    if opcode == 0x9:
                        ws._send_frame(0xA, payload)
                        continue
                    if opcode not in {0x1, 0x2}:
                        continue
                    try:
                        message = json.loads(payload.decode("utf-8", errors="ignore"))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(message, list) or len(message) < 2:
                        continue
                    if message[0] != "stdout":
                        continue
                    text = str(message[1] or "")
                    output.feed(text)
                    try:
                        if writer is not None:
                            writer.write(text)
                        if on_output is not None:
                            on_output(text)
                    except BaseException:
                        callback_failed = True
                        raise
                    if not sent and output.scanner.prompt:
                        sent = True
                        _send(ws)
                    if sent and output.scanner.returncode is not None:
                        break
        except JobShellAuthError as error:
            if callback_failed or sent or output.buffer.total:
                raise
            raise SessionExpiredError(str(error)) from error
        except Exception:
            if callback_failed:
                raise
            logger.debug("JupyterTerminal WebSocket failed", exc_info=True)
    result = output.result()
    if not result.completed:
        logger.debug(
            "JupyterTerminal command ended without a completion marker; sent=%s output_chars=%s",
            sent,
            output.buffer.total,
        )
    return result


def _jupyter_ws_headers(session: WebSession, ws_url: str) -> dict[str, str]:
    parsed = urlsplit(ws_url)
    origin_scheme = "https" if parsed.scheme == "wss" else "http"
    headers = {
        "Origin": f"{origin_scheme}://{parsed.netloc}",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    cookie_pairs: list[str] = []
    cookies = session.storage_state.get("cookies") if session.storage_state else None
    if isinstance(cookies, list):
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "").strip()
            value = str(cookie.get("value") or "").strip()
            if name and value:
                cookie_pairs.append(f"{name}={value}")
    for name, value in (session.cookies or {}).items():
        if name and value:
            pair = f"{name}={value}"
            if pair not in cookie_pairs:
                cookie_pairs.append(pair)
    if cookie_pairs:
        headers["Cookie"] = "; ".join(cookie_pairs)
    return headers


def _send_jupyter_stdin(ws: _TextWebSocket, text: str) -> None:
    ws.send_text(json.dumps(["stdin", text]))


def _run_command_capture_in_notebook_sync(
    *,
    notebook_id: str,
    command: str,
    session: Optional[WebSession],
    timeout: float,
    marker: str | None,
    on_output: Callable[[str], None] | None = None,
    max_output_bytes: int | None = DEFAULT_MAX_OUTPUT_BYTES,
    output_to: OutputTarget = None,
    capture: bool = True,
) -> JupyterCommandResult:
    if session is None:
        session = get_web_session()

    effective_marker = marker or new_completion_marker()
    from inspire.platform.web.runtime import active_transport

    owner = active_transport.get()
    cli_compat = owner is None or owner.cli_compat
    deadline = time.monotonic() + timeout

    def _unfinished() -> JupyterCommandResult:
        return JupyterCommandResult(
            returncode=MISSING_MARKER_RETURN_CODE,
            output="",
            completed=False,
            marker=effective_marker,
        )

    creation_timeout = max(int(timeout), 10) if cli_compat else max(timeout, 0.001)
    with _jupyter_terminal(session, notebook_id, timeout_s=creation_timeout) as term:
        if term is None:
            return _unfinished()
        if not cli_compat and time.monotonic() >= deadline:
            return _unfinished()
        options: dict[str, Any] = {}
        if on_output is not None:
            options["on_output"] = on_output
        if max_output_bytes != DEFAULT_MAX_OUTPUT_BYTES or output_to is not None or not capture:
            options.update(max_output_bytes=max_output_bytes, output_to=output_to, capture=capture)
        result = _capture_terminal_output(
            ws_url=term.ws_url,
            session=session,
            stdin_data=build_jupyter_exec_command(command, marker=effective_marker),
            timeout_ms=(
                max(int(timeout * 1000), 1000)
                if cli_compat
                else max(1, int((deadline - time.monotonic()) * 1000))
            ),
            marker=effective_marker,
            **options,
        )
        return result if result is not None else _unfinished()

async def _capture_terminal_output_async(
    *,
    ws_url: str,
    session: WebSession,
    stdin_data: str,
    timeout_ms: int,
    marker: str,
    on_output: Callable[[str], Any] | None = None,
    max_output_bytes: int | None = DEFAULT_MAX_OUTPUT_BYTES,
    output_to: OutputTarget = None,
    capture: bool = True,
) -> Optional[JupyterCommandResult]:
    """Run one command on the terminal and read back everything it printed.

    A Python port of what used to run as JavaScript inside a Playwright page.
    The protocol is unchanged: wait for a prompt (or give up waiting and send
    anyway), feed stdin in chunks, and stop as soon as the marker line carries
    an exit code.
    """
    from inspire.platform.web.pty_socket import AsyncWebSocketClient

    deadline = time.monotonic() + max(int(timeout_ms), 1) / 1000.0
    # Bounded wait for the prompt: a terminal that never prints one still has
    # to receive the command, or the call would return empty on a timeout.
    prompt_deadline = time.monotonic() + max(0, min(timeout_ms - 500, 3000)) / 1000.0
    validate_capture(max_output_bytes, capture, output_to)
    output = TerminalOutput(marker, max_output_bytes, capture)
    pending: asyncio.Task[tuple[int, bytes]] | None = None
    sent = False
    callback_failed = False

    async def _send(ws: AsyncWebSocketClient) -> None:
        for start in range(0, len(stdin_data), _STDIN_CHUNK):
            await ws.send_text(json.dumps(["stdin", stdin_data[start : start + _STDIN_CHUNK]]))
            if start + _STDIN_CHUNK < len(stdin_data):
                await asyncio.sleep(_STDIN_CHUNK_DELAY_S)

    async with async_output_writer(output_to) as writer:
        try:
            async with AsyncWebSocketClient(
                ws_url, _jupyter_ws_headers(session, ws_url), timeout=max(timeout_ms / 1000, 0.001)
            ) as ws:
                while True:
                    now = time.monotonic()
                    if now >= deadline:
                        break
                    if not sent and now >= prompt_deadline:
                        sent = True
                        await _send(ws)
                    if pending is None:
                        pending = asyncio.create_task(ws.recv_frame())
                    ready, _ = await asyncio.wait({pending}, timeout=min(0.25, deadline - now))
                    if not ready:
                        continue
                    try:
                        opcode, payload = pending.result()
                    except EOFError:
                        break
                    finally:
                        pending = None
                    if opcode == 0x8:
                        break
                    if opcode == 0x9:
                        await ws.send_pong(payload)
                        continue
                    if opcode not in {0x1, 0x2}:
                        continue
                    try:
                        message = json.loads(payload.decode("utf-8", errors="ignore"))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(message, list) or len(message) < 2:
                        continue
                    if message[0] != "stdout":
                        continue
                    text = str(message[1] or "")
                    output.feed(text)
                    try:
                        if writer is not None:
                            await writer.write(text)
                        if on_output is not None:
                            await deliver_output(on_output, text)
                    except BaseException:
                        callback_failed = True
                        raise
                    if not sent and output.scanner.prompt:
                        sent = True
                        await _send(ws)
                    if sent and output.scanner.returncode is not None:
                        break
        except JobShellAuthError as error:
            if callback_failed or sent or output.buffer.total:
                raise
            raise SessionExpiredError(str(error)) from error
        except (EOFError, OSError):
            if callback_failed:
                raise
            logger.debug("JupyterTerminal WebSocket failed", exc_info=True)
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    result = output.result()
    if not result.completed:
        logger.debug(
            "JupyterTerminal command ended without a completion marker; sent=%s output_chars=%s",
            sent,
            output.buffer.total,
        )
    return result



async def run_command_capture_in_notebook_async(
    *, notebook_id: str, command: str, session: WebSession,
    timeout: float = 60, marker: str | None = None,
    on_output: Callable[[str], Any] | None = None,
    max_output_bytes: int | None = DEFAULT_MAX_OUTPUT_BYTES,
    output_to: OutputTarget = None, capture: bool = True,
) -> JupyterCommandResult:
    from inspire.platform.web.transport_core import ApplicationRequest
    from inspire.platform.web.browser_api.notebooks import _v2_result, _notebooks_referer

    owner = get_transport(session)
    effective_marker = marker or new_completion_marker()
    deadline = time.monotonic() + timeout
    unfinished = JupyterCommandResult(124, "", False, effective_marker)
    payload = _v2_result(await owner.request_async(
        "POST", "/api/v2/notebook?Action=GetNotebookAccessUrl",
        body={"notebook_id": notebook_id}, referer=_notebooks_referer(), timeout=timeout,
    ))
    lab_url = str(payload.get("jupyter_url") or "").strip()
    if not lab_url:
        return unfinished
    base = rtunnel_module.jupyter_server_base(lab_url)
    term_name = ""
    async with owner.application_connection_async(lab_url) as http:
        async def send(method: str, url: str, **options: Any) -> Any:
            options.setdefault("allow_redirects", False)
            return await owner.request_async(
                method, url, body=ApplicationRequest(http, options), timeout=timeout,
            )

        try:
            entrance = await send("GET", lab_url, allow_redirects=True)
            if entrance.status_code == 401:
                raise SessionExpiredError("Jupyter terminal session expired (401).")
            xsrf = str(http.cookies.get("_xsrf") or "")
            response = await send(
                "POST", f"{base}api/terminals",
                headers={"X-XSRFToken": xsrf} if xsrf else {},
            )
            if response.status_code == 401:
                raise SessionExpiredError("Jupyter terminal session expired (401).")
            if response.status_code not in (200, 201):
                return unfinished
            term_name = str(response.json().get("name") or "")
            if not term_name or time.monotonic() >= deadline:
                return unfinished
            result = await _capture_terminal_output_async(
                ws_url=rtunnel_module.build_terminal_websocket_url(lab_url, term_name),
                session=session, stdin_data=build_jupyter_exec_command(command, marker=effective_marker),
                timeout_ms=max(1, int((deadline - time.monotonic()) * 1000)),
                marker=effective_marker, on_output=on_output, max_output_bytes=max_output_bytes,
                output_to=output_to, capture=capture,
            )
            return result if result is not None else unfinished
        finally:
            if term_name:
                with contextlib.suppress(Exception):
                    await send(
                        "DELETE", f"{base}api/terminals/{term_name}",
                        headers={"X-XSRFToken": str(http.cookies.get("_xsrf") or "")},
                    )
