"""Browser-free command execution and capture shared by SDK workloads.

PTY and Jupyter use completion markers because a closed socket does not prove
that the command finished. SSH uses its local process exit status, which can
also report a connection failure. Neither closing a connection nor cancelling
an async reader guarantees that a dispatched remote command stopped.

Notebook auto selection only probes cached bridges; bridge creation belongs to
the CLI. Synchronous and native async readers share command construction and
capture rules, with callbacks delivered in order and full sinks independent of
the bounded in-memory capture. File transfer publication belongs to
inspire.services.execution.notebook_transfer.
"""

from __future__ import annotations

from inspire.platform.web.flow import Program, workflow, call, blocking_call, perform_sync
from inspire.services.execution.async_output import deliver_output

import asyncio
import codecs
import re
import select
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Any, Sequence

from inspire.platform.web.pty_socket import (
    WebSocketClient,
    AsyncWebSocketClient,
    JobShellAuthError,
    build_remote_cmd_headers,
    normalize_job_instances,
    select_job_instance,
)
from inspire.platform.web.session import WebSession
from inspire.platform.web.session.models import SessionExpiredError
from inspire.platform.web.browser_api.jupyter_terminal import (
    build_jupyter_exec_command,
    new_completion_marker,
    TerminalOutput,
    run_command_capture_in_notebook,
    run_command_capture_in_notebook_async,
)
from inspire.bridge.tunnel.config import load_tunnel_config
from inspire.bridge.tunnel.ssh_exec import run_ssh_command_streaming
from inspire.services.execution.async_output import async_output_writer
from inspire.exec_output import (
    DEFAULT_MAX_OUTPUT_BYTES,
    OutputTarget,
    OutputBuffer,
    output_writer,
    validate_capture,
)
from inspire.bridge import tunnel
from inspire.services.execution.notebook_targets import read_target_cache


@dataclass(frozen=True)
class ExecResult:
    """Transport completion and bounded decoded output, not proof of workload health.

    completed distinguishes a recognized command end from timeout/disconnection;
    an SSH connection error can still produce a local exit status. PTY output is
    merged, while SSH can retain separate stdout/stderr. A truncated capture may
    coexist with a complete output sink; total_output_bytes counts decoded output
    as UTF-8 bytes before capture trimming, not network traffic.
    """

    returncode: int
    output: str
    stdout: str
    stderr: str
    completed: bool
    transport: str
    instance: str = ""
    truncated: bool = False
    total_output_bytes: int = field(default=0, compare=False)


def build_remote_command(
    command: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    remote_env_exports: str = "",
) -> str:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string.")
    if env is not None and not isinstance(env, dict):
        raise ValueError("env must be a dictionary of string values.")
    exports = remote_env_exports
    for key, value in (env or {}).items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid environment key: {key!r}")
        if not isinstance(value, str):
            raise ValueError(f"Environment value for {key!r} must be a string.")
        exports += f"export {key}={shlex.quote(value)} && "
    if cwd is not None:
        if not isinstance(cwd, str):
            raise ValueError("cwd must be a string.")
        # Preserve the CLI's double-quoted shape while treating a caller's path literally.
        quoted = re.sub(r'([\\"$`])', r"\\\1", cwd)
        exports += f'cd "{quoted}" && '
    return exports + command


def exec_over_pty_websocket(
    *,
    session: WebSession,
    url: str,
    command: str,
    timeout: float,
    marker: str | None = None,
    on_output: Callable[[str], None] | None = None,
    max_output_bytes: int | None = DEFAULT_MAX_OUTPUT_BYTES,
    output_to: OutputTarget = None,
    capture: bool = True,
) -> ExecResult:
    marker = marker or new_completion_marker()
    deadline = time.monotonic() + timeout
    prompt_deadline = time.monotonic() + min(3.0, timeout / 4)
    validate_capture(max_output_bytes, capture, output_to)
    output = TerminalOutput(marker, max_output_bytes, capture)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    sent = False
    ws = WebSocketClient(
        url, build_remote_cmd_headers(session, base_url=session.base_url), timeout=timeout
    )
    with output_writer(output_to) as writer:
        try:
            try:
                ws.connect()
            except TimeoutError:
                return ExecResult(124, "", "", "", False, "pty")
            except JobShellAuthError as error:
                raise SessionExpiredError(str(error)) from error
            while True:
                now = time.monotonic()
                if now >= deadline:
                    break
                if not sent and now >= prompt_deadline:
                    ws.send_text(
                        build_jupyter_exec_command(command, marker=marker).rstrip("\r") + "\r"
                    )
                    sent = True
                ready, _, _ = select.select([ws.fileno()], [], [], min(0.25, deadline - now))
                if not ready and not ws.has_pending_data():
                    continue
                ws.set_read_timeout(max(0.001, deadline - time.monotonic()))
                try:
                    opcode, payload = ws.recv_frame()
                except (EOFError, TimeoutError):
                    break
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    ws.send_pong(payload)
                    continue
                if opcode not in (0x1, 0x2):
                    continue
                chunk = decoder.decode(payload)
                output.feed(chunk)
                if writer is not None:
                    writer.write(chunk)
                if on_output is not None and chunk:
                    on_output(chunk)
                if not sent and output.scanner.prompt:
                    ws.send_text(
                        build_jupyter_exec_command(command, marker=marker).rstrip("\r") + "\r"
                    )
                    sent = True
                if output.scanner.returncode is not None:
                    break
        finally:
            ws.close()
        final = decoder.decode(b"", final=True)
        output.feed(final)
        if final:
            if writer is not None:
                writer.write(final)
            if on_output is not None:
                on_output(final)
        result = output.result()
        return ExecResult(
            result.returncode,
            result.output,
            result.output,
            "",
            result.completed,
            "pty",
            truncated=result.truncated,
            total_output_bytes=result.total_output_bytes,
        )


def exec_in_notebook_jupyter(
    *,
    session: WebSession,
    notebook_id: str,
    command: str,
    timeout: float,
    marker: str | None = None,
    on_output: Callable[[str], None] | None = None,
    max_output_bytes: int | None = DEFAULT_MAX_OUTPUT_BYTES,
    output_to: OutputTarget = None,
    capture: bool = True,
) -> ExecResult:
    from requests.exceptions import Timeout

    validate_capture(max_output_bytes, capture, output_to)
    partial = OutputBuffer(max_output_bytes, capture=capture)
    callback_failed = False

    def observe(chunk: str) -> None:
        nonlocal callback_failed
        partial.feed(chunk)
        if on_output is not None:
            try:
                on_output(chunk)
            except BaseException:
                callback_failed = True
                raise

    try:
        result = run_command_capture_in_notebook(
            session=session,
            notebook_id=notebook_id,
            command=command,
            timeout=timeout,
            marker=marker,
            on_output=observe,
            max_output_bytes=max_output_bytes,
            output_to=output_to,
            capture=capture,
        )
    except (Timeout, TimeoutError):
        if callback_failed:
            raise
        text = partial.text()
        return ExecResult(
            124,
            text,
            text,
            "",
            False,
            "jupyter",
            truncated=partial.truncated,
            total_output_bytes=partial.total,
        )
    return ExecResult(
        result.returncode,
        result.output,
        result.output,
        "",
        result.completed,
        "jupyter",
        truncated=result.truncated,
        total_output_bytes=result.total_output_bytes,
    )


def exec_in_notebook_ssh(
    *,
    bridge_name: str,
    account: str,
    command: str,
    timeout: float,
    on_output: Callable[[str], None] | None = None,
    max_output_bytes: int | None = DEFAULT_MAX_OUTPUT_BYTES,
    output_to: OutputTarget = None,
    capture: bool = True,
) -> ExecResult:
    validate_capture(max_output_bytes, capture, output_to)
    config = load_tunnel_config(account=account)
    # Share the return budget equally between the two separately retained streams.
    limit = None if max_output_bytes is None else max_output_bytes // 2
    out, err = OutputBuffer(limit, capture=capture), OutputBuffer(limit, capture=capture)
    callback_failed = False
    with output_writer(output_to) as writer:

        def observe(chunk: str, buffer: OutputBuffer) -> None:
            nonlocal callback_failed
            buffer.feed(chunk)
            try:
                if writer is not None:
                    perform_sync(blocking_call(writer.write, chunk))
                if on_output is not None:
                    perform_sync(call(on_output, chunk))
            except BaseException:
                callback_failed = True
                raise

        completed = True
        try:
            code = perform_sync(call(run_ssh_command_streaming,
                command,
                bridge_name=bridge_name,
                config=config,
                timeout=timeout,
                output_callback=lambda chunk: observe(chunk, out),
                stderr_callback=lambda chunk: observe(chunk, err),
            ))
        except subprocess.TimeoutExpired:
            if callback_failed:
                raise
            code, completed = 124, False
    stdout, stderr = out.text(), err.text()
    return ExecResult(
        code,
        stdout + stderr,
        stdout,
        stderr,
        completed,
        "ssh",
        truncated=out.truncated or err.truncated,
        total_output_bytes=out.total + err.total,
    )


@workflow
def cached_notebook_bridge(*, notebook_id: str, workspace_id: str, account: str) -> Program[str | None]:
    """Find only an existing bridge for the exact account/workspace/notebook identity."""
    try:
        config = yield blocking_call(load_tunnel_config, account=account)
        entries = (yield blocking_call(read_target_cache)).get("targets", {})
        names = [
            row.get("bridge_name")
            for row in entries.values()
            if isinstance(row, dict)
            and row.get("account") == account
            and row.get("notebook_id") == notebook_id
            and row.get("workspace_id") == workspace_id
        ]
        bridges = list(config.list_bridges())
        bridges.sort(key=lambda bridge: bridge.name not in names)
        for bridge in bridges:
            if bridge.notebook_id != notebook_id or bridge.workspace_id != workspace_id:
                continue
            if (yield call(tunnel.is_tunnel_available,
                bridge_name=bridge.name,
                config=config,
                retries=0,
                retry_pause=0.0,
                progressive=False,
            )):
                return bridge.name
    except (OSError, ValueError, RuntimeError):
        return None
    return None


def select_exec_instance(
    workload: str, rows: Sequence[dict[str, Any]], instance: str | None = None
) -> str:
    """Select a single addressable running instance using the workload's public labels."""
    from inspire.services.job.job_events import job_instance_views, select_job_instance_views
    from inspire.services.hpc.hpc_instances import hpc_instance_views, select_hpc_instance_views
    from inspire.services.ray.ray_instances import ray_instance_views, select_ray_instance_views
    from inspire.services.serving.serving_instances import (
        serving_instance_views,
        select_serving_instance_views,
    )

    if instance is not None and (not isinstance(instance, str) or not instance.strip()):
        raise ValueError("instance must be a non-empty label or instance name.")
    if workload == "job":
        normalized = normalize_job_instances(list(rows))
        if instance is None or any(i.name == instance for i in normalized):
            return select_job_instance(normalized, instance_name=instance, prompt=False).name
        rank_text = instance.removeprefix("rank=")
        if rank_text.isdigit():
            return select_job_instance(normalized, rank=int(rank_text), prompt=False).name
    running = [
        row
        for row in rows
        if "run" in str(row.get("status") or row.get("instance_status") or "").lower()
    ]
    selectors: dict[str, tuple[Callable[..., Any], Callable[..., Any]]] = {
        "job": (job_instance_views, select_job_instance_views),
        "hpc": (hpc_instance_views, select_hpc_instance_views),
        "ray": (ray_instance_views, select_ray_instance_views),
        "serving": (serving_instance_views, select_serving_instance_views),
    }
    project, choose = selectors[workload]
    # Construct labels before filtering so positional ranks remain stable.
    views = project(rows)
    handles = {str(row.get("name") or row.get("pod_name") or "") for row in running}
    views = [view for view in views if view.handle in handles]
    if not views:
        raise ValueError(f"No running instances found for {workload}.")
    if instance:
        chosen = [view for view in views if view.handle == instance]
        if not chosen:
            chosen = choose(views, [instance])
    elif workload == "serving":
        chosen = views[:1]
    else:
        default = "launcher" if workload == "hpc" else "head"
        chosen = choose(views, [default])
    if len(chosen) != 1:
        candidates = ", ".join(f"{v.label} ({v.handle})" for v in chosen or views)
        raise ValueError(
            f"Multiple running instances match; pass instance. Candidates: {candidates}"
        )
    return chosen[0].handle

async def exec_over_pty_websocket_async(
    *,
    session: WebSession,
    url: str,
    command: str,
    timeout: float,
    marker: str | None = None,
    on_output: Callable[[str], Any] | None = None,
    max_output_bytes: int | None = DEFAULT_MAX_OUTPUT_BYTES,
    output_to: OutputTarget = None,
    capture: bool = True,
) -> ExecResult:
    marker = marker or new_completion_marker()
    deadline = time.monotonic() + timeout
    prompt_deadline = time.monotonic() + min(3.0, timeout / 4)
    validate_capture(max_output_bytes, capture, output_to)
    output = TerminalOutput(marker, max_output_bytes, capture)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending: asyncio.Task[tuple[int, bytes]] | None = None
    sent = False
    ws = AsyncWebSocketClient(
        url, build_remote_cmd_headers(session, base_url=session.base_url), timeout=timeout
    )
    async with async_output_writer(output_to) as writer:
        try:
            try:
                await ws.connect()
            except TimeoutError:
                return ExecResult(124, "", "", "", False, "pty")
            except JobShellAuthError as error:
                raise SessionExpiredError(str(error)) from error
            while True:
                now = time.monotonic()
                if now >= deadline:
                    break
                if not sent and now >= prompt_deadline:
                    await ws.send_text(
                        build_jupyter_exec_command(command, marker=marker).rstrip("\r") + "\r"
                    )
                    sent = True
                if pending is None:
                    pending = asyncio.create_task(ws.recv_frame())
                ready, _ = await asyncio.wait({pending}, timeout=min(0.25, deadline - now))
                if not ready:
                    continue
                try:
                    opcode, payload = pending.result()
                except (EOFError, TimeoutError):
                    break
                finally:
                    pending = None
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    await ws.send_pong(payload)
                    continue
                if opcode not in (0x1, 0x2):
                    continue
                chunk = decoder.decode(payload)
                output.feed(chunk)
                if writer is not None:
                    await writer.write(chunk)
                if on_output is not None and chunk:
                    await deliver_output(on_output, chunk)
                if not sent and output.scanner.prompt:
                    await ws.send_text(
                        build_jupyter_exec_command(command, marker=marker).rstrip("\r") + "\r"
                    )
                    sent = True
                if output.scanner.returncode is not None:
                    break
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await ws.close()
        final = decoder.decode(b"", final=True)
        output.feed(final)
        if final:
            if writer is not None:
                await writer.write(final)
            if on_output is not None:
                await deliver_output(on_output, final)
        result = output.result()
        return ExecResult(
            result.returncode,
            result.output,
            result.output,
            "",
            result.completed,
            "pty",
            truncated=result.truncated,
            total_output_bytes=result.total_output_bytes,
        )


async def exec_in_notebook_jupyter_async(
    *,
    session: WebSession,
    notebook_id: str,
    command: str,
    timeout: float,
    marker: str | None = None,
    on_output: Callable[[str], Any] | None = None,
    max_output_bytes: int | None = DEFAULT_MAX_OUTPUT_BYTES,
    output_to: OutputTarget = None,
    capture: bool = True,
) -> ExecResult:
    from requests.exceptions import Timeout

    validate_capture(max_output_bytes, capture, output_to)
    partial = OutputBuffer(max_output_bytes, capture=capture)
    callback_failed = False

    async def observe(chunk: str) -> None:
        nonlocal callback_failed
        partial.feed(chunk)
        if on_output is not None:
            try:
                await deliver_output(on_output, chunk)
            except BaseException:
                callback_failed = True
                raise

    try:
        result = await run_command_capture_in_notebook_async(
            session=session,
            notebook_id=notebook_id,
            command=command,
            timeout=timeout,
            marker=marker,
            on_output=observe,
            max_output_bytes=max_output_bytes,
            output_to=output_to,
            capture=capture,
        )
    except (Timeout, TimeoutError):
        if callback_failed:
            raise
        text = partial.text()
        return ExecResult(
            124,
            text,
            text,
            "",
            False,
            "jupyter",
            truncated=partial.truncated,
            total_output_bytes=partial.total,
        )
    return ExecResult(
        result.returncode,
        result.output,
        result.output,
        "",
        result.completed,
        "jupyter",
        truncated=result.truncated,
        total_output_bytes=result.total_output_bytes,
    )
