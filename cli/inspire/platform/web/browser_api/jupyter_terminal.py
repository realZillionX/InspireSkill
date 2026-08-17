from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import select
import signal
import shlex
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Iterator, Protocol, Optional
from urllib.parse import urlsplit

from inspire.cli.utils.terminal_io import write_stream_output
from inspire.platform.web.browser_api import rtunnel as rtunnel_module
from inspire.platform.web.browser_api.core import (
    _in_asyncio_loop,
    _run_in_thread,
)
from inspire.platform.web.session import WebSession
from inspire.platform.web.session import build_requests_session, get_web_session

if sys.platform != "win32":
    import termios
    import tty

JUPYTER_DONE_PREFIX = "__INSPIRE_JUPYTER_DONE_"
MISSING_MARKER_RETURN_CODE = 124
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# The terminal echoes a shell prompt when it is ready for input.
_PROMPT_RE = re.compile(r"[$#]\s*$")
_EXIT_CODE_RE = re.compile(r"^\d+\s")
# The PTY drops input that arrives faster than it drains, so feed it in chunks.
_STDIN_CHUNK = 2048
_STDIN_CHUNK_DELAY_S = 0.05


class _TextWebSocket(Protocol):
    def send_text(self, text: str) -> None: ...


@dataclass(frozen=True)
class JupyterCommandResult:
    returncode: int
    output: str
    completed: bool
    marker: str


def new_completion_marker() -> str:
    return f"{JUPYTER_DONE_PREFIX}{uuid.uuid4().hex}"


def build_jupyter_exec_command(command: str, *, marker: str) -> str:
    script = "\n".join(
        [
            "set +e",
            "(",
            command,
            ")",
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
            output=raw_output,
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


def build_jupyter_terminal_ws_url(lab_url: str, term_name: str) -> str:
    return rtunnel_module._build_terminal_websocket_url(lab_url, term_name)


def build_shell_bootstrap(*, cwd: str | None, env_exports: str) -> str:
    """Build the login-shell bootstrap for an interactive JupyterTerminal.

    The shell runs as a child rather than via ``exec`` so that the surviving
    parent can announce the exit — the gateway keeps the websocket open after
    the shell is gone, so nothing else tells the client to stop reading. See
    ``job_shell.SHELL_EXIT_MARKER``.
    """
    from inspire.cli.utils.job_shell import shell_exit_announce

    tail = f"$SHELL -l; {shell_exit_announce()}\r"
    if cwd:
        return f"{env_exports}cd {shlex.quote(cwd)} && {tail}"
    return f"{env_exports}{tail}"


def run_command_capture_in_notebook(
    *,
    notebook_id: str,
    command: str,
    session: Optional[WebSession] = None,
    timeout: int = 60,
    marker: str | None = None,
) -> JupyterCommandResult:
    if _in_asyncio_loop():
        return _run_in_thread(
            _run_command_capture_in_notebook_sync,
            notebook_id=notebook_id,
            command=command,
            session=session,
            timeout=timeout,
            marker=marker,
        )
    return _run_command_capture_in_notebook_sync(
        notebook_id=notebook_id,
        command=command,
        session=session,
        timeout=timeout,
        marker=marker,
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
        yield None
        return

    http = build_requests_session(session, lab_url)
    term_name = ""
    base = rtunnel_module._jupyter_server_base(lab_url)
    try:
        http.get(lab_url, timeout=(5, timeout_s), allow_redirects=True)
        xsrf = str(http.cookies.get("_xsrf") or "")
        headers = {"X-XSRFToken": xsrf} if xsrf else {}
        response = http.post(f"{base}api/terminals", headers=headers, timeout=(5, timeout_s))
        if response.status_code not in (200, 201):
            yield None
            return
        term_name = str(response.json().get("name") or "")
        if not term_name:
            yield None
            return
        yield _JupyterTerminal(
            lab_url=lab_url,
            name=term_name,
            ws_url=rtunnel_module._build_terminal_websocket_url(lab_url, term_name),
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
            http.close()


def _capture_terminal_output(
    *,
    ws_url: str,
    session: WebSession,
    stdin_data: str,
    timeout_ms: int,
    marker: str,
) -> Optional[JupyterCommandResult]:
    """Run one command on the terminal and read back everything it printed.

    A Python port of what used to run as JavaScript inside a Playwright page.
    The protocol is unchanged: wait for a prompt (or give up waiting and send
    anyway), feed stdin in chunks, and stop as soon as the marker line carries
    an exit code.
    """
    from inspire.cli.utils.job_shell import _WebSocketClient

    deadline = time.monotonic() + max(int(timeout_ms), 1000) / 1000.0
    # Bounded wait for the prompt: a terminal that never prints one still has
    # to receive the command, or the call would return empty on a timeout.
    prompt_deadline = time.monotonic() + max(0, min(timeout_ms - 500, 3000)) / 1000.0
    done_prefix = f"{marker}:exit:"
    output = ""
    sent = False

    def _send(ws: _TextWebSocket) -> None:
        for start in range(0, len(stdin_data), _STDIN_CHUNK):
            _send_jupyter_stdin(ws, stdin_data[start : start + _STDIN_CHUNK])
            if start + _STDIN_CHUNK < len(stdin_data):
                time.sleep(_STDIN_CHUNK_DELAY_S)

    try:
        with _WebSocketClient(ws_url, _jupyter_ws_headers(session, ws_url)) as ws:
            while True:
                now = time.monotonic()
                if now >= deadline:
                    break
                if not sent and now >= prompt_deadline:
                    sent = True
                    _send(ws)
                ready, _, _ = select.select([ws], [], [], min(0.25, deadline - now))
                if not ready:
                    continue
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
                output += text
                if not sent and _PROMPT_RE.search(text):
                    sent = True
                    _send(ws)
                if sent:
                    done_at = output.find(done_prefix)
                    if done_at >= 0 and _EXIT_CODE_RE.match(output[done_at + len(done_prefix) :]):
                        break
    except Exception:
        return None
    return parse_jupyter_exec_output(output, marker=marker)


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


def _run_jupyter_terminal_shell(
    *,
    ws_url: str,
    session: WebSession,
    bootstrap: str,
    stdin=None,  # noqa: ANN001
    stdout=None,  # noqa: ANN001
) -> int:
    from inspire.cli.utils.job_shell import (
        CTRL_RIGHT_BRACKET,
        ShellExitWatcher,
        _WebSocketClient,
        _stty_command,
    )

    if sys.platform == "win32":
        raise RuntimeError("Interactive Jupyter terminal shells are not yet supported on native Windows; use `inspire notebook exec` instead.")

    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stdout_buffer = getattr(stdout, "buffer", stdout)
    headers = _jupyter_ws_headers(session, ws_url)
    old_term = None
    raw_mode = bool(getattr(stdin, "isatty", lambda: False)())

    with _WebSocketClient(ws_url, headers) as ws:
        _send_jupyter_stdin(ws, bootstrap)
        _send_jupyter_stdin(ws, _stty_command().replace("\n", "\r"))

        def resize_handler(signum, frame):  # noqa: ANN001
            del signum, frame
            try:
                _send_jupyter_stdin(ws, _stty_command().replace("\n", "\r"))
            except Exception:
                pass

        previous_winch = None
        if raw_mode:
            old_term = termios.tcgetattr(stdin.fileno())
            tty.setraw(stdin.fileno())
            previous_winch = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, resize_handler)
        try:
            stdin_open = True
            exit_watcher = ShellExitWatcher()
            while True:
                readers = [ws]
                if stdin_open and not getattr(stdin, "closed", False):
                    readers.append(stdin)
                ready, _, _ = select.select(readers, [], [])
                if ws in ready:
                    try:
                        opcode, payload = ws.recv_frame()
                    except EOFError:
                        return 0
                    if opcode == 0x8:
                        return 0
                    if opcode == 0x9:
                        ws._send_frame(0xA, payload)
                        continue
                    if opcode in {0x1, 0x2}:
                        text = payload.decode("utf-8", errors="ignore")
                        try:
                            msg = json.loads(text)
                        except json.JSONDecodeError:
                            stream = payload
                        else:
                            if not (
                                isinstance(msg, list)
                                and len(msg) >= 2
                                and msg[0] == "stdout"
                            ):
                                continue
                            stream = str(msg[1] or "").encode()
                        visible, shell_exited = exit_watcher.feed(stream)
                        if visible:
                            write_stream_output(stdout_buffer, visible)
                        if shell_exited:
                            return 0
                if stdin in ready:
                    data = os.read(stdin.fileno(), 4096)
                    if not data:
                        stdin_open = False
                        continue
                    if CTRL_RIGHT_BRACKET in data:
                        return 0
                    _send_jupyter_stdin(ws, data.decode("utf-8", errors="ignore"))
        finally:
            if raw_mode and old_term is not None:
                termios.tcsetattr(stdin.fileno(), termios.TCSADRAIN, old_term)
                if previous_winch is not None:
                    signal.signal(signal.SIGWINCH, previous_winch)


def open_jupyter_terminal_shell(
    *,
    notebook_id: str,
    session: Optional[WebSession] = None,
    cwd: str | None = None,
    env_exports: str = "",
    timeout: int = 60,
) -> int:
    active_session = session or get_web_session()
    with _jupyter_terminal(active_session, notebook_id, timeout_s=max(int(timeout), 10)) as term:
        if term is None:
            return MISSING_MARKER_RETURN_CODE
        return _run_jupyter_terminal_shell(
            ws_url=term.ws_url,
            session=active_session,
            bootstrap=build_shell_bootstrap(cwd=cwd, env_exports=env_exports),
        )


def _run_command_capture_in_notebook_sync(
    *,
    notebook_id: str,
    command: str,
    session: Optional[WebSession],
    timeout: int,
    marker: str | None,
) -> JupyterCommandResult:
    if session is None:
        session = get_web_session()

    effective_marker = marker or new_completion_marker()
    timeout_ms = max(int(timeout * 1000), 1000)

    def _unfinished() -> JupyterCommandResult:
        return JupyterCommandResult(
            returncode=MISSING_MARKER_RETURN_CODE,
            output="",
            completed=False,
            marker=effective_marker,
        )

    with _jupyter_terminal(session, notebook_id, timeout_s=max(int(timeout), 10)) as term:
        if term is None:
            return _unfinished()
        result = _capture_terminal_output(
            ws_url=term.ws_url,
            session=session,
            stdin_data=build_jupyter_exec_command(command, marker=effective_marker),
            timeout_ms=timeout_ms,
            marker=effective_marker,
        )
        return result if result is not None else _unfinished()
