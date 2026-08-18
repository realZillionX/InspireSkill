"""Byte-stream helpers for interactive terminal commands.

``run_interactive_pty`` has two implementations rather than one with platform
branches inside it. Almost everything the POSIX version touches — ``pty.fork``,
``termios``, ``SIGWINCH``, ``killpg`` — simply does not exist on Windows, so
splitting at the top keeps each version readable, and type-checkable, on its own
terms instead of threading conditionals through a hundred lines.
"""

from __future__ import annotations

import errno
import os
import select
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Sequence
from types import TracebackType
from typing import BinaryIO

if sys.platform != "win32":
    import fcntl
    import pty
    import termios
    import tty


def write_stream_output(stream: BinaryIO, payload: bytes | str) -> None:
    """Write one stream chunk through to the terminal verbatim.

    Interactive streams are passed through byte for byte. Rewriting them
    costs a working terminal: withholding a chunk tail to wait for a possible
    identifier suppresses keystroke echo in raw mode, truncates shell
    prompts, and leaves full-screen programs half-painted.
    """
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    if data:
        stream.write(data)
        stream.flush()


if sys.platform == "win32":

    def run_interactive_pty(
        argv: Sequence[str],
        *,
        stdin=None,  # noqa: ANN001
        stdout=None,  # noqa: ANN001
    ) -> int:
        """Run an interactive command with this process's stdio inherited.

        There is no PTY to fork here, and none is needed: the only caller runs
        ``ssh``, and native OpenSSH allocates its own console for an interactive
        session. The child drives the terminal directly, so there is nothing to
        proxy — which is why *stdin* and *stdout* are ignored rather than wired
        up to something that could only be a worse terminal.
        """
        del stdin, stdout
        if not argv:
            raise ValueError("Interactive command cannot be empty.")
        return subprocess.run(list(argv), check=False).returncode

else:

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

    def run_interactive_pty(
        argv: Sequence[str],
        *,
        stdin=None,  # noqa: ANN001
        stdout=None,  # noqa: ANN001
    ) -> int:
        """Run an interactive command in a PTY, proxying stdio verbatim."""
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
                    write_stream_output(stdout_buffer, payload)

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
    "run_interactive_pty",
    "write_stream_output",
]
