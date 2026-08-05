"""Safe byte-stream helpers for interactive terminal commands."""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
import tty
from collections.abc import Sequence
from types import TracebackType
from typing import BinaryIO

from inspire.cli.utils.raw_ids import RawIdStreamScrubber


def write_scrubbed_output(
    stream: BinaryIO,
    scrubber: RawIdStreamScrubber,
    payload: bytes | str,
) -> None:
    """Write one sanitized stream chunk without breaking ANSI sequences."""
    safe_payload = scrubber.feed(payload)
    if safe_payload:
        stream.write(safe_payload)
        stream.flush()


def flush_scrubbed_output(
    stream: BinaryIO,
    scrubber: RawIdStreamScrubber,
) -> None:
    """Write any sanitized suffix retained across stream chunks."""
    safe_payload = scrubber.flush()
    if safe_payload:
        stream.write(safe_payload)
        stream.flush()


def _copy_terminal_size(source_fd: int, target_fd: int) -> None:
    try:
        size = fcntl.ioctl(source_fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, columns, xpixel, ypixel = struct.unpack("HHHH", size)
        packed = struct.pack("HHHH", rows, columns, xpixel, ypixel)
        fcntl.ioctl(target_fd, termios.TIOCSWINSZ, packed)
    except OSError:
        pass


def _signal_pty_child(pid: int, sig: int) -> None:
    try:
        process_group = os.getpgid(pid)
        if process_group == pid:
            os.killpg(process_group, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        return


def _wait_pty_child(pid: int, *, terminate: bool) -> int:
    if not terminate:
        _, status = os.waitpid(pid, 0)
        return status

    _signal_pty_child(pid, signal.SIGTERM)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        try:
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return 0
        if waited_pid == pid:
            return status
        time.sleep(0.01)

    _signal_pty_child(pid, signal.SIGKILL)
    try:
        _, status = os.waitpid(pid, 0)
    except ChildProcessError:
        return 0
    return status


def run_scrubbed_pty(
    argv: Sequence[str],
    *,
    stdin=None,  # noqa: ANN001
    stdout=None,  # noqa: ANN001
) -> int:
    """Run an interactive command in a PTY while sanitizing child output."""
    if not argv:
        raise ValueError("Interactive command cannot be empty.")

    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stdout_buffer = getattr(stdout, "buffer", stdout)
    stdin_fd = stdin.fileno()
    raw_mode = bool(getattr(stdin, "isatty", lambda: False)())

    pid, master_fd = pty.fork()
    if pid == 0:
        os.execvp(argv[0], list(argv))
        raise RuntimeError("unreachable")

    scrubber = RawIdStreamScrubber()
    old_term = None
    previous_winch = None
    failure: tuple[BaseException, TracebackType | None] | None = None
    status = 0

    def resize_handler(signum, frame):  # noqa: ANN001
        del signum, frame
        _copy_terminal_size(stdin_fd, master_fd)

    try:
        if raw_mode:
            old_term = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)
            _copy_terminal_size(stdin_fd, master_fd)
            previous_winch = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, resize_handler)

        stdin_open = True
        while True:
            readers = [master_fd]
            if stdin_open and not getattr(stdin, "closed", False):
                readers.append(stdin_fd)
            ready, _, _ = select.select(readers, [], [])

            if master_fd in ready:
                try:
                    payload = os.read(master_fd, 4096)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    payload = b""
                if not payload:
                    break
                write_scrubbed_output(stdout_buffer, scrubber, payload)

            if stdin_fd in ready:
                payload = os.read(stdin_fd, 4096)
                if payload:
                    os.write(master_fd, payload)
                else:
                    stdin_open = False
    except BaseException as exc:
        failure = (exc, exc.__traceback__)
    finally:
        for cleanup in (
            lambda: flush_scrubbed_output(stdout_buffer, scrubber),
            lambda: (
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_term)
                if raw_mode and old_term is not None
                else None
            ),
            lambda: (
                signal.signal(signal.SIGWINCH, previous_winch)
                if raw_mode and previous_winch is not None
                else None
            ),
            lambda: os.close(master_fd),
        ):
            try:
                cleanup()
            except BaseException as exc:
                if failure is None:
                    failure = (exc, exc.__traceback__)
        try:
            status = _wait_pty_child(pid, terminate=failure is not None)
        except BaseException as exc:
            if failure is None:
                failure = (exc, exc.__traceback__)

    if failure is not None:
        error, traceback = failure
        raise error.with_traceback(traceback)
    return os.waitstatus_to_exitcode(status)


__all__ = [
    "flush_scrubbed_output",
    "run_scrubbed_pty",
    "write_scrubbed_output",
]
