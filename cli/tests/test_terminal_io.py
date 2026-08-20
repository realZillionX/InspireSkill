from __future__ import annotations

import io
import os
import sys

import pytest

from inspire.cli.utils import terminal_io
from inspire.cli.utils.terminal_io import run_interactive_pty


# run_interactive_pty forks a real PTY. Windows has none, so the function falls
# back to a plain subprocess there and these tests have nothing to drive.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY only")


class _PipeInput:
    def __init__(self, fd: int) -> None:
        self._fd = fd
        self.closed = False

    def fileno(self) -> int:
        return self._fd

    def isatty(self) -> bool:
        return False


def test_run_interactive_pty_passes_child_output_through_verbatim() -> None:
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    stdout = io.BytesIO()
    try:
        returncode = run_interactive_pty(
            [
                "/bin/sh",
                "-c",
                "printf '\\033[32mok\\033[0m job-1234abcd\\n'; exit 7",
            ],
            stdin=_PipeInput(read_fd),
            stdout=stdout,
        )
    finally:
        os.close(read_fd)

    assert returncode == 7
    assert stdout.getvalue() == b"\x1b[32mok\x1b[0m job-1234abcd\r\n"


def test_run_interactive_pty_emits_a_partial_line_without_waiting() -> None:
    """A prompt with no trailing newline has to reach the terminal now.

    Holding the tail of a chunk back to inspect it for identifiers is what
    swallows keystroke echo in raw mode and leaves prompts half-written.
    """
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    stdout = io.BytesIO()
    try:
        run_interactive_pty(
            ["/bin/sh", "-c", "printf 'root@trainer:~/work'"],
            stdin=_PipeInput(read_fd),
            stdout=stdout,
        )
    finally:
        os.close(read_fd)

    assert stdout.getvalue() == b"root@trainer:~/work"


def test_run_interactive_pty_terminates_and_reaps_child_on_output_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracked: dict[str, int] = {}
    real_fork = terminal_io.pty.fork

    def tracked_fork() -> tuple[int, int]:
        pid, master_fd = real_fork()
        if pid > 0:
            tracked["pid"] = pid
        return pid, master_fd

    def fail_output(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise RuntimeError("output failed")

    monkeypatch.setattr(terminal_io.pty, "fork", tracked_fork)
    monkeypatch.setattr(terminal_io, "write_stream_output", fail_output)

    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    try:
        with pytest.raises(RuntimeError, match="output failed"):
            run_interactive_pty(
                ["/bin/sh", "-c", "printf boom; sleep 10"],
                stdin=_PipeInput(read_fd),
                stdout=io.BytesIO(),
            )
    finally:
        os.close(read_fd)

    with pytest.raises(ChildProcessError):
        os.waitpid(tracked["pid"], os.WNOHANG)
