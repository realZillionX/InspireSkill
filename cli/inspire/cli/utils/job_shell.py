"""Interactive CLI adapters for remote terminal sessions."""

from __future__ import annotations

import contextlib
import shutil
import sys
import click
from inspire.cli.utils.interactive_console import ShellStreams, raw_terminal, watch_terminal_resize
from inspire.cli.utils.terminal_io import write_stream_output
from inspire.platform.web.browser_api.core import _get_base_url
from inspire.platform.web.session import WebSession, get_web_session
from inspire.platform.web import pty_socket
from inspire.platform.web.pty_socket import (
    WebSocketClient as _WebSocketClient,
    JobShellAuthError,
    JobInstance,
    ShellExitWatcher,
    SHELL_BOOTSTRAP,
    CTRL_RIGHT_BRACKET,
    RUNNING_INSTANCE_STATUS,
)


def build_remote_cmd_ws_url(job_id: str, instance_name: str, *, workload: str = "job") -> str:
    return pty_socket.build_remote_cmd_ws_url(
        job_id, instance_name, workload=workload, base_url=_get_base_url()
    )


def build_remote_cmd_headers(session: WebSession) -> dict[str, str]:
    return pty_socket.build_remote_cmd_headers(session, base_url=_get_base_url())


def select_job_instance(
    instances: list[JobInstance],
    *,
    instance_name: str | None = None,
    rank: int | None = None,
    prompt: bool = False,
) -> JobInstance:
    running = [i for i in instances if i.status.lower() == RUNNING_INSTANCE_STATUS]
    if prompt and not instance_name and rank is None and len(running) > 1:
        click.echo("Multiple running instances found:")
        for index, inst in enumerate(running, 1):
            click.echo(f"  {index}. {inst.name} (rank={inst.rank})")
        choice = click.prompt(
            "Select instance", type=click.IntRange(1, len(running)), default=1, show_default=True
        )
        return running[choice - 1]
    return pty_socket.select_job_instance(instances, instance_name=instance_name, rank=rank)


def _terminal_size() -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=(80, 24))
    return size.columns, size.lines


def _stty_command() -> str:
    columns, rows = _terminal_size()
    return f"stty columns {columns} rows {rows}\n"


def run_remote_shell(
    *,
    job_id: str,
    instance_name: str,
    session: WebSession,
    workload: str = "job",
    stdin=None,  # noqa: ANN001
    stdout=None,  # noqa: ANN001
    websocket_cls: type[_WebSocketClient] = _WebSocketClient,
) -> int:
    """Open the remote PTY websocket and proxy local stdio."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stdout_buffer = getattr(stdout, "buffer", stdout)
    ws_url = build_remote_cmd_ws_url(job_id, instance_name, workload=workload)
    headers = build_remote_cmd_headers(session)

    with websocket_cls(ws_url, headers) as ws:
        ws.send_text(SHELL_BOOTSTRAP)
        ws.send_text(_stty_command())

        def announce_resize() -> None:
            # A missed resize notification must not interrupt the interactive shell.
            with contextlib.suppress(Exception):
                ws.send_text(_stty_command())

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
                        visible, shell_exited = exit_watcher.feed(payload)
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
                    ws.send_text(keystrokes.decode("utf-8", errors="ignore"))


def open_job_shell(
    *,
    job_id: str,
    instance_name: str,
    session: WebSession | None = None,
    workload: str = "job",
    websocket_cls: type[_WebSocketClient] = _WebSocketClient,
) -> int:
    """Open a job shell, refreshing the web session once after a 401 handshake."""
    active_session = session or get_web_session()
    try:
        return run_remote_shell(
            job_id=job_id,
            instance_name=instance_name,
            session=active_session,
            workload=workload,
            websocket_cls=websocket_cls,
        )
    except JobShellAuthError:
        refreshed = get_web_session(force_refresh=True)
        return run_remote_shell(
            job_id=job_id,
            instance_name=instance_name,
            session=refreshed,
            workload=workload,
            websocket_cls=websocket_cls,
        )
