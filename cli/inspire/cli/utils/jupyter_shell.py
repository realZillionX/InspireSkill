"""Interactive Jupyter terminal CLI adapter."""

from __future__ import annotations
import contextlib
import json
import sys
from typing import Optional
from inspire.cli.utils.terminal_io import write_stream_output
from inspire.platform.web.session import WebSession, get_web_session
from inspire.platform.web.browser_api.jupyter_terminal import (
    _jupyter_terminal,
    _jupyter_ws_headers,
    _send_jupyter_stdin,
    build_shell_bootstrap,
    MISSING_MARKER_RETURN_CODE,
)


def _run_jupyter_terminal_shell(
    *,
    ws_url: str,
    session: WebSession,
    bootstrap: str,
    stdin=None,  # noqa: ANN001
    stdout=None,  # noqa: ANN001
) -> int:
    from inspire.platform.web.pty_socket import (
        CTRL_RIGHT_BRACKET,
        ShellExitWatcher,
        WebSocketClient as _WebSocketClient,
    )
    from inspire.cli.utils.job_shell import _stty_command

    from inspire.cli.utils.interactive_console import (
        ShellStreams,
        raw_terminal,
        watch_terminal_resize,
    )

    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stdout_buffer = getattr(stdout, "buffer", stdout)
    headers = _jupyter_ws_headers(session, ws_url)

    with _WebSocketClient(ws_url, headers) as ws:
        _send_jupyter_stdin(ws, bootstrap)
        _send_jupyter_stdin(ws, _stty_command().replace("\n", "\r"))

        def announce_resize() -> None:
            # A missed resize notification must not interrupt the interactive terminal.
            with contextlib.suppress(Exception):
                _send_jupyter_stdin(ws, _stty_command().replace("\n", "\r"))

        streams = ShellStreams(ws, stdin)
        with raw_terminal(stdin), watch_terminal_resize(stdin, announce_resize) as poll_resize:
            stdin_open = True
            exit_watcher = ShellExitWatcher()
            while True:
                poll_resize()
                socket_ready, keystrokes = streams.wait(stdin_open=stdin_open)
                if socket_ready:
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
                            if not (isinstance(msg, list) and len(msg) >= 2 and msg[0] == "stdout"):
                                continue
                            stream = str(msg[1] or "").encode()
                        visible, shell_exited = exit_watcher.feed(stream)
                        if visible:
                            write_stream_output(stdout_buffer, visible)
                        if shell_exited:
                            return 0
                if keystrokes is not None:
                    if not keystrokes:
                        stdin_open = False
                        continue
                    if CTRL_RIGHT_BRACKET in keystrokes:
                        return 0
                    _send_jupyter_stdin(ws, keystrokes.decode("utf-8", errors="ignore"))


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
