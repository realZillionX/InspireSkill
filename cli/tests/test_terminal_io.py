from __future__ import annotations

import io
import os

import pytest

from inspire.cli.utils import terminal_io
from inspire.cli.utils.terminal_io import run_scrubbed_pty


class _PipeInput:
    def __init__(self, fd: int) -> None:
        self._fd = fd
        self.closed = False

    def fileno(self) -> int:
        return self._fd

    def isatty(self) -> bool:
        return False


def test_run_scrubbed_pty_preserves_terminal_output_without_handles() -> None:
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    stdout = io.BytesIO()
    try:
        returncode = run_scrubbed_pty(
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
    assert stdout.getvalue() == b"\x1b[32mok\x1b[0m <redacted>\r\n"


def test_run_scrubbed_pty_terminates_and_reaps_child_on_output_failure(
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
    monkeypatch.setattr(terminal_io, "write_scrubbed_output", fail_output)

    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    try:
        with pytest.raises(RuntimeError, match="output failed"):
            run_scrubbed_pty(
                ["/bin/sh", "-c", "printf boom; sleep 10"],
                stdin=_PipeInput(read_fd),
                stdout=io.BytesIO(),
            )
    finally:
        os.close(read_fd)

    with pytest.raises(ChildProcessError):
        os.waitpid(tracked["pid"], os.WNOHANG)
