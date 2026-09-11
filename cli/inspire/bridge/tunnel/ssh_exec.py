"""SSH command execution via ProxyCommand: run, stream, and argument helpers."""

from __future__ import annotations

from inspire.platform.web.flow import blocking_io, perform_sync, call

import logging
import shlex
import subprocess
import time
from typing import Callable, Optional

from inspire.process_io import iter_process_lines

from .config import load_tunnel_config
from .models import (
    BridgeNotFoundError,
    BridgeProfile,
    TunnelConfig,
    TunnelNotAvailableError,
)
from .rtunnel import _ensure_rtunnel_binary
from .ssh import _get_proxy_command, build_ssh_process_env

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


@blocking_io
def _resolve_bridge_and_proxy(
    bridge_name: Optional[str],
    config: Optional[TunnelConfig],
    *,
    quiet: bool = True,
) -> tuple[TunnelConfig, BridgeProfile, str]:
    if config is None:
        config = load_tunnel_config()

    bridge = config.get_bridge(bridge_name)
    if not bridge:
        if bridge_name:
            raise BridgeNotFoundError(f"Bridge '{bridge_name}' not found")
        raise TunnelNotAvailableError(
            "No notebook connection is cached. Run "
            "'inspire notebook connection refresh <notebook> "
            "--workspace <workspace>' first."
        )

    _ensure_rtunnel_binary(config)
    proxy_cmd = _get_proxy_command(bridge, config.rtunnel_bin, quiet=quiet)
    return config, bridge, proxy_cmd


def _build_stdin_script(command: str) -> str:
    """Build a short shell script to pipe into ``bash -l`` via stdin.

    This avoids embedding *command* in the SSH process's command-line
    arguments, which would otherwise make ``pkill -f <pattern>`` match
    the parent bash process and tear down the SSH session.
    """
    return f"export LC_ALL=C LANG=C; {command}\n"


def _wrap_remote_command(command: str) -> str:
    """Wrap a remote command for SSH argv-based execution."""
    return f"bash -l -c {shlex.quote(command)}"


def _build_ssh_base_args(
    *,
    bridge: BridgeProfile,
    proxy_cmd: str,
    batch_mode: bool = True,
) -> list[str]:
    args = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        f"ProxyCommand={proxy_cmd}",
        "-o",
        "LogLevel=ERROR",
        "-p",
        str(bridge.ssh_port),
        f"{bridge.ssh_user}@localhost",
    ]
    if batch_mode:
        args[5:5] = ["-o", "BatchMode=yes"]
    return args


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_ssh_command(
    command: str,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: Optional[float] = None,
    capture_output: bool = True,
    check: bool = False,
    *,
    quiet_proxy: bool = True,
    pass_stdin: bool = False,
) -> subprocess.CompletedProcess:
    """Execute a command on Bridge via SSH ProxyCommand."""
    _config, bridge, proxy_cmd = _resolve_bridge_and_proxy(bridge_name, config, quiet=quiet_proxy)
    ssh_cmd = _build_ssh_base_args(bridge=bridge, proxy_cmd=proxy_cmd)
    input_payload: Optional[str] = None
    if pass_stdin:
        ssh_cmd.append(_wrap_remote_command(command))
    else:
        ssh_cmd.append("bash -l")
        input_payload = _build_stdin_script(command)

    logger.debug(
        (
            "run_ssh_command bridge=%s timeout=%s capture_output=%s "
            "quiet_proxy=%s pass_stdin=%s command=%s"
        ),
        bridge.name,
        timeout,
        capture_output,
        quiet_proxy,
        pass_stdin,
        command,
    )

    result = subprocess.run(
        ssh_cmd,
        input=input_payload,
        capture_output=capture_output,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=check,
        env=build_ssh_process_env(),
    )
    logger.debug(
        "run_ssh_command completed bridge=%s returncode=%s",
        bridge.name,
        result.returncode,
    )
    if capture_output:
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        logger.debug(
            "run_ssh_command output bridge=%s stdout_chars=%s stderr_chars=%s",
            bridge.name,
            len(stdout),
            len(stderr),
        )
        if stdout:
            logger.debug("run_ssh_command stdout:\n%s", stdout)
        if stderr:
            logger.debug("run_ssh_command stderr:\n%s", stderr)
    return result


# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------


def get_ssh_command_args(
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    remote_command: Optional[str] = None,
) -> list[str]:
    """Build SSH command arguments with ProxyCommand."""
    _config, bridge, proxy_cmd = _resolve_bridge_and_proxy(bridge_name, config)
    args = _build_ssh_base_args(bridge=bridge, proxy_cmd=proxy_cmd, batch_mode=False)
    if remote_command:
        args.append(remote_command)
    return args


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------


def run_ssh_command_streaming(
    command: str,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: Optional[float] = None,
    output_callback: Optional[Callable[[str], None]] = None,
    *,
    pass_stdin: bool = False,
    stderr_callback: Optional[Callable[[str], None]] = None,
) -> int:
    """Execute a command on Bridge via SSH with streaming output."""
    _config, bridge, proxy_cmd = _resolve_bridge_and_proxy(bridge_name, config)
    ssh_cmd = _build_ssh_base_args(bridge=bridge, proxy_cmd=proxy_cmd)
    if pass_stdin:
        ssh_cmd.append(_wrap_remote_command(command))
    else:
        ssh_cmd.append("bash -l")

    logger.debug(
        "run_ssh_command_streaming bridge=%s timeout=%s pass_stdin=%s command=%s",
        bridge.name,
        timeout,
        pass_stdin,
        command,
    )

    if output_callback is None:

        def _default_output_callback(line: str) -> None:
            # Remote command output is the user's own data: pass it through.
            import click

            click.echo(line, nl=False)

        output_callback = _default_output_callback

    return perform_sync(call(
        _stream_process, ssh_cmd, _build_stdin_script(command) if not pass_stdin else None,
        output_callback, stderr_callback, timeout, build_ssh_process_env(), bridge.name,
    ))


def _stream_process(ssh_cmd, script, output_callback, stderr_callback, timeout, env, bridge_name):
    pass_stdin = script is None
    popen_stdin = None if pass_stdin else subprocess.PIPE
    process = subprocess.Popen(
        ssh_cmd,
        stdin=popen_stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if stderr_callback is not None else subprocess.STDOUT,
        bufsize=1,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    stdout = process.stdout
    if stdout is None:
        raise RuntimeError("SSH process stdout pipe was not created")

    # Feed the command via stdin so it never appears in the process cmdline.
    if not pass_stdin:
        if process.stdin is not None:
            process.stdin.write(script)
            process.stdin.close()

    if stderr_callback is not None:
        return _stream_separate_pipes(process, output_callback, stderr_callback, timeout, ssh_cmd)

    start_time = time.time()

    try:
        for line in iter_process_lines(process, stdout):
            if timeout is not None:
                elapsed = time.time() - start_time
                if elapsed >= timeout:
                    logger.debug(
                        "run_ssh_command_streaming timeout bridge=%s elapsed=%.2fs limit=%ss",
                        bridge_name,
                        elapsed,
                        timeout,
                    )
                    process.terminate()
                    process.wait()
                    raise subprocess.TimeoutExpired(ssh_cmd, timeout)

            if line is not None:
                logger.debug("run_ssh_command_streaming line=%s", line.rstrip("\n"))
                output_callback(line)

        process.wait()
        logger.debug(
            "run_ssh_command_streaming completed bridge=%s returncode=%s",
            bridge_name,
            process.returncode,
        )
        return process.returncode

    except KeyboardInterrupt:
        logger.debug("run_ssh_command_streaming interrupted bridge=%s", bridge_name)
        process.terminate()
        process.wait()
        raise
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()


__all__ = [
    "build_ssh_process_env",
    "_build_ssh_base_args",
    "_build_stdin_script",
    "_wrap_remote_command",
    "_resolve_bridge_and_proxy",
    "get_ssh_command_args",
    "run_ssh_command",
    "run_ssh_command_streaming",
]


def _stream_separate_pipes(
    process: subprocess.Popen,
    output_callback: Callable[[str], None],
    stderr_callback: Callable[[str], None],
    timeout: float | None,
    command: list[str],
) -> int:
    """Drain both pipes without blocking the deadline on a partial line."""
    import codecs
    import queue
    import threading

    pending: queue.Queue[tuple[int, bytes | None]] = queue.Queue(maxsize=16)
    stopped = threading.Event()

    def enqueue(item):
        while not stopped.is_set():
            try:
                pending.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def drain(index, stream):
        try:
            while not stopped.is_set():
                chunk = stream.buffer.read1(4096)
                if not chunk:
                    break
                enqueue((index, chunk))
        finally:
            enqueue((index, None))

    readers = [
        threading.Thread(target=drain, args=(i, stream), daemon=True)
        for i, stream in enumerate((process.stdout, process.stderr))
    ]
    decoders = [codecs.getincrementaldecoder("utf-8")("replace") for _ in readers]
    callbacks = [output_callback, stderr_callback]
    deadline = time.monotonic() + timeout if timeout is not None else None
    for reader in readers:
        reader.start()
    ended = 0
    try:
        while ended < 2:
            remaining = deadline - time.monotonic() if deadline is not None else 1.0
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout or 0)
            try:
                index, data = pending.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            chunk = decoders[index].decode(data or b"", final=data is None)
            if chunk:
                callbacks[index](chunk)
            if data is None:
                ended += 1
        return process.wait(timeout=max(0.001, deadline - time.monotonic()) if deadline else None)
    finally:
        stopped.set()
        if process.poll() is None:
            process.kill()
            process.wait()
        for reader in readers:
            reader.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
