"""The terminal plumbing behind ``job shell`` and the Jupyter terminal.

The loop these back is the one place where Windows and POSIX cannot share an
implementation, so both branches are exercised here — the Windows one by forcing
the platform and driving the reader thread with a fake stream.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from typing import Any

import pytest

from inspire.cli.utils.interactive_console import (
    ResizePoller,
    ShellStreams,
    raw_terminal,
    watch_terminal_resize,
)


class FakeStdin:
    """A stdin whose bytes the Windows reader thread can drain."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def read(self, size: int) -> bytes:
        del size
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def isatty(self) -> bool:
        return False


class FakeTty(FakeStdin):
    """Same, but claiming to be a terminal — for the resize watcher's gate."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def socket_pair() -> Any:
    left, right = socket.socketpair()
    try:
        yield left, right
    finally:
        left.close()
        right.close()


def _drain_until(streams: ShellStreams, predicate: Any, *, limit: float = 2.0) -> list[Any]:
    """Poll ``wait`` until *predicate* holds, so the thread has time to deliver."""
    deadline = time.monotonic() + limit
    seen: list[Any] = []
    while time.monotonic() < deadline:
        result = streams.wait(stdin_open=True)
        seen.append(result)
        if predicate(result):
            return seen
    raise AssertionError(f"condition never held; saw {seen!r}")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX select/SIGWINCH only")
def test_posix_wait_reports_socket_and_keystrokes_from_one_select(socket_pair: Any) -> None:
    left, right = socket_pair
    read_fd, write_fd = os.pipe()
    try:
        stdin = os.fdopen(read_fd, "rb", buffering=0)
        os.write(write_fd, b"ls\r")
        right.sendall(b"x")

        streams = ShellStreams(left, stdin)
        socket_ready, keystrokes = streams.wait(stdin_open=True)

        assert socket_ready is True
        assert keystrokes == b"ls\r"
    finally:
        os.close(write_fd)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX select/SIGWINCH only")
def test_posix_wait_skips_stdin_once_it_is_closed(socket_pair: Any) -> None:
    left, right = socket_pair
    right.sendall(b"x")
    read_fd, write_fd = os.pipe()
    try:
        stdin = os.fdopen(read_fd, "rb", buffering=0)
        os.write(write_fd, b"ignored")

        streams = ShellStreams(left, stdin)
        socket_ready, keystrokes = streams.wait(stdin_open=False)

        assert socket_ready is True
        assert keystrokes is None
    finally:
        os.close(write_fd)


def test_windows_wait_takes_keystrokes_from_the_reader_thread(
    socket_pair: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # select() cannot watch a console handle on Windows, so stdin arrives out of
    # band and the socket wait becomes a short tick instead of a block.
    monkeypatch.setattr("sys.platform", "win32")
    left, _right = socket_pair
    streams = ShellStreams(left, FakeStdin([b"echo hi\r"]))

    seen = _drain_until(streams, lambda result: result[1] == b"echo hi\r")

    # Every tick before the keystroke landed reported "nothing yet", not a hang.
    assert all(result[0] is False for result in seen)


def test_windows_wait_signals_end_of_input_with_an_empty_chunk(
    socket_pair: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    left, _right = socket_pair
    streams = ShellStreams(left, FakeStdin([]))

    _drain_until(streams, lambda result: result[1] == b"")


def test_windows_wait_reports_a_ready_socket(
    socket_pair: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    left, right = socket_pair
    right.sendall(b"frame")
    streams = ShellStreams(left, FakeStdin([]))

    _drain_until(streams, lambda result: result[0] is True)


def test_raw_terminal_is_a_no_op_for_a_non_tty() -> None:
    with raw_terminal(FakeStdin([])):
        pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX select/SIGWINCH only")
def test_resize_watch_fires_on_posix_signal() -> None:
    import signal

    fired: list[int] = []
    with watch_terminal_resize(FakeTty([]), lambda: fired.append(1)) as poll:
        # POSIX gets a real signal, so the polling hook has nothing to do.
        poll()
        assert fired == []
        signal.raise_signal(signal.SIGWINCH)

    assert fired == [1]


def test_resize_poller_fires_once_per_size_change(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stands in for SIGWINCH on Windows. The platform split is at import time,
    # so the poller is tested directly rather than through the context manager.
    current = os.terminal_size((80, 24))
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback=None: current)

    fired: list[int] = []
    poll = ResizePoller(lambda: fired.append(1))

    poll()
    assert fired == []

    current = os.terminal_size((100, 30))
    poll()
    assert fired == [1]

    # A steady size does not re-announce.
    poll()
    assert fired == [1]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX select/SIGWINCH only")
def test_resize_watch_restores_the_previous_posix_handler() -> None:
    import signal

    original = signal.getsignal(signal.SIGWINCH)
    with watch_terminal_resize(FakeTty([]), lambda: None):
        assert signal.getsignal(signal.SIGWINCH) is not original
    assert signal.getsignal(signal.SIGWINCH) is original


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX select/SIGWINCH only")
def test_resize_watch_leaves_signal_state_alone_for_a_non_tty() -> None:
    import signal

    # The pre-split implementation registered SIGWINCH inside `if raw_mode:`.
    # Losing that gate broke library callers: signal.signal is main-thread-only,
    # so a worker thread driving the shell with a piped stdin died on
    # `ValueError: signal only works in main thread` before reading a byte.
    original = signal.getsignal(signal.SIGWINCH)
    with watch_terminal_resize(FakeStdin([]), lambda: None) as poll:
        assert signal.getsignal(signal.SIGWINCH) is original
        poll()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX select/SIGWINCH only")
def test_resize_watch_works_from_a_worker_thread_when_stdin_is_piped() -> None:
    import threading

    failures: list[BaseException] = []

    def drive() -> None:
        try:
            with watch_terminal_resize(FakeStdin([]), lambda: None) as poll:
                poll()
        except BaseException as exc:  # noqa: BLE001 — the failure IS the assertion
            failures.append(exc)

    worker = threading.Thread(target=drive)
    worker.start()
    worker.join(timeout=5)

    assert failures == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX select/SIGWINCH only")
def test_resize_watch_leaves_signal_state_alone_for_a_worker_thread_tty() -> None:
    import signal
    import threading

    original = signal.getsignal(signal.SIGWINCH)
    failures: list[BaseException] = []

    def drive() -> None:
        try:
            with watch_terminal_resize(FakeTty([]), lambda: None) as poll:
                poll()
        except BaseException as exc:  # noqa: BLE001 — the failure IS the assertion
            failures.append(exc)

    worker = threading.Thread(target=drive)
    worker.start()
    worker.join(timeout=5)

    assert failures == []
    assert signal.getsignal(signal.SIGWINCH) is original
