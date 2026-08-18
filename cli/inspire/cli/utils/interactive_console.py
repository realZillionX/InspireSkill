"""Terminal plumbing shared by the websocket-backed interactive shells.

``job shell`` and the Jupyter terminal both need the same three things from the
local terminal: put it in raw mode, read keystrokes as they arrive, and notice
when the window is resized. POSIX gives all three directly (``tty.setraw``,
``select`` on stdin, ``SIGWINCH``). Windows gives none of them:

- there is no ``termios``; raw mode is ``SetConsoleMode`` with the line, echo and
  processed-input flags cleared and virtual-terminal input enabled;
- ``select`` accepts only sockets, so stdin has to be drained by a thread;
- there is no ``SIGWINCH``, so a resize is only visible by polling the size.

Both implementations live here so the two shells stay one code path.
"""

from __future__ import annotations

import os
import queue
import select
import shutil
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Callable

# Windows console-mode flags (consoleapi.h). Named here so the raw-mode switch
# reads as intent rather than as a hex literal.
_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_LINE_INPUT = 0x0002
_ENABLE_ECHO_INPUT = 0x0004
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

_WINDOWS_POLL_INTERVAL = 0.02

if sys.platform != "win32":
    import termios
    import tty


@contextmanager
def raw_terminal(stream: Any) -> Iterator[None]:
    """Put the terminal in raw mode for the block, then restore it.

    A no-op when *stream* is not a terminal, so piped and captured runs behave
    the same as they did before.
    """
    if not bool(getattr(stream, "isatty", lambda: False)()):
        yield
        return

    if sys.platform == "win32":
        with _raw_windows_console():
            yield
        return

    descriptor = stream.fileno()
    saved = termios.tcgetattr(descriptor)
    tty.setraw(descriptor)
    try:
        yield
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)


@contextmanager
def _raw_windows_console() -> Iterator[None]:
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    stdin_handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
    stdout_handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)

    saved_input = ctypes.c_uint32()
    saved_output = ctypes.c_uint32()
    have_input = bool(kernel32.GetConsoleMode(stdin_handle, ctypes.byref(saved_input)))
    have_output = bool(kernel32.GetConsoleMode(stdout_handle, ctypes.byref(saved_output)))
    if not have_input:
        # Not a real console (a ConPTY-less redirect, or a handle we cannot
        # touch). Leave it alone rather than fail the shell.
        yield
        return

    # Clearing PROCESSED_INPUT is what makes Ctrl-C arrive as a 0x03 byte
    # instead of a signal, matching tty.setraw: the remote shell handles it.
    raw_input_mode = saved_input.value & ~(
        _ENABLE_PROCESSED_INPUT | _ENABLE_LINE_INPUT | _ENABLE_ECHO_INPUT
    )
    raw_input_mode |= _ENABLE_VIRTUAL_TERMINAL_INPUT
    kernel32.SetConsoleMode(stdin_handle, raw_input_mode)
    if have_output:
        kernel32.SetConsoleMode(
            stdout_handle, saved_output.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING
        )
    try:
        yield
    finally:
        kernel32.SetConsoleMode(stdin_handle, saved_input.value)
        if have_output:
            kernel32.SetConsoleMode(stdout_handle, saved_output.value)


@contextmanager
def watch_terminal_resize(on_resize: Callable[[], None]) -> Iterator[Callable[[], None]]:
    """Install a resize notifier, yielding a poll callback for the event loop.

    POSIX gets ``SIGWINCH`` and the yielded callback does nothing. Windows has no
    such signal, so the callback compares the current size against the last one
    and fires *on_resize* when it changed; the shells call it once per tick.
    """
    if sys.platform == "win32":
        last_size = shutil.get_terminal_size(fallback=(80, 24))

        def poll() -> None:
            nonlocal last_size
            current = shutil.get_terminal_size(fallback=(80, 24))
            if current != last_size:
                last_size = current
                on_resize()

        yield poll
        return

    def handler(signum: int, frame: Any) -> None:
        del signum, frame
        on_resize()

    previous = signal.getsignal(signal.SIGWINCH)
    signal.signal(signal.SIGWINCH, handler)
    try:
        yield lambda: None
    finally:
        signal.signal(signal.SIGWINCH, previous)


class ShellStreams:
    """The wait step of a websocket shell loop, for one websocket and stdin.

    ``wait()`` returns ``(socket_ready, keystrokes)``. *keystrokes* is ``None``
    when nothing arrived, and ``b""`` once stdin is at end of input.

    POSIX watches both in one blocking ``select``. Windows cannot: ``select``
    there takes sockets only, so stdin is drained by a thread and the loop ticks
    on a short socket timeout instead of blocking. The tick is also what makes
    resize polling possible, since there is no SIGWINCH.
    """

    def __init__(self, socket: Any, stdin: Any) -> None:
        self._socket = socket
        self._stdin = stdin
        self._queue: queue.Queue[bytes] | None = None
        if sys.platform == "win32":
            self._queue = queue.Queue()
            threading.Thread(
                target=self._drain, daemon=True, name="inspire-stdin-reader"
            ).start()

    def _drain(self) -> None:
        assert self._queue is not None
        # ReadConsoleW honours ENABLE_VIRTUAL_TERMINAL_INPUT, so arrow keys and
        # other escape sequences arrive as the same bytes a POSIX tty produces.
        buffer = getattr(self._stdin, "buffer", self._stdin)
        source = getattr(buffer, "raw", None) or buffer
        try:
            while True:
                chunk = source.read(4096)
                if not chunk:
                    break
                self._queue.put(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self._queue.put(b"")

    def wait(self, *, stdin_open: bool) -> tuple[bool, bytes | None]:
        if self._queue is not None:
            socket_ready, _, _ = select.select([self._socket], [], [], _WINDOWS_POLL_INTERVAL)
            keystrokes: bytes | None = None
            if stdin_open:
                try:
                    keystrokes = self._queue.get_nowait()
                except queue.Empty:
                    keystrokes = None
            return bool(socket_ready), keystrokes

        readers: list[Any] = [self._socket]
        if stdin_open and not getattr(self._stdin, "closed", False):
            readers.append(self._stdin)
        ready, _, _ = select.select(readers, [], [])
        keystrokes = os.read(self._stdin.fileno(), 4096) if self._stdin in ready else None
        return self._socket in ready, keystrokes


__all__ = [
    "ShellStreams",
    "raw_terminal",
    "watch_terminal_resize",
]
