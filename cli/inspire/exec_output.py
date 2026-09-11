"""Bounded decoded output capture and incremental terminal scanning."""

from __future__ import annotations

from inspire.platform.web.flow import blocking_call, perform_sync

import os
import re
from collections import deque
from contextlib import contextmanager
from typing import IO, Iterator

DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
ELISION = "\n[... output truncated ...]\n"
OutputTarget = str | os.PathLike[str] | IO[str] | None


def validate_capture(max_output_bytes: int | None, capture: bool, output_to: OutputTarget) -> None:
    if max_output_bytes is not None and (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or max_output_bytes < 2 * len(ELISION.encode())
    ):
        raise ValueError(
            f"max_output_bytes must be None or an integer >= {2 * len(ELISION.encode())}."
        )
    if not isinstance(capture, bool):
        raise ValueError("capture must be a bool.")
    if output_to is not None and not isinstance(output_to, (str, os.PathLike)):
        if not callable(getattr(output_to, "write", None)):
            raise ValueError("output_to must be a path or writable text file.")


@contextmanager
def output_writer(target: OutputTarget) -> Iterator[IO[str] | None]:
    if isinstance(target, (str, os.PathLike)):
        stream = None

        def open_stream():
            nonlocal stream
            stream = open(target, "w", encoding="utf-8", newline="")

        try:
            perform_sync(blocking_call(open_stream))
            yield stream
        finally:
            if stream is not None:
                perform_sync(blocking_call(stream.close))
    else:
        yield target


class OutputBuffer:
    """Keep an initial prefix and a rolling suffix, with amortized linear writes."""

    def __init__(self, limit: int | None = DEFAULT_MAX_OUTPUT_BYTES, *, capture: bool = True):
        self.limit = limit
        self.capture = capture
        self.total = 0
        self.head = bytearray()
        self.tail: deque[memoryview] = deque()
        self.tail_size = 0

    def feed(self, text: str) -> None:
        data = text.encode("utf-8")
        self.total += len(data)
        if not self.capture or not data:
            return
        if self.limit is None:
            self.tail.append(memoryview(data))
            return
        head_size = self.limit // 2
        take = min(len(data), head_size - len(self.head))
        self.head.extend(data[:take])
        data = data[take:]
        tail_limit = self.limit - head_size
        if len(data) >= tail_limit:
            self.tail.clear()
            self.tail_size = 0
            data = data[-tail_limit:]
        if data:
            self.tail.append(memoryview(data))
            self.tail_size += len(data)
        while self.tail_size > tail_limit:
            excess = self.tail_size - tail_limit
            first = self.tail.popleft()
            if len(first) > excess:
                self.tail.appendleft(first[excess:])
                self.tail_size -= excess
            else:
                self.tail_size -= len(first)

    @property
    def truncated(self) -> bool:
        return self.capture and self.limit is not None and self.total > self.limit

    def text(self) -> str:
        if not self.capture:
            return ""
        tail = b"".join(self.tail)
        if not self.truncated:
            return (bytes(self.head) + tail).decode("utf-8", errors="ignore")
        assert self.limit is not None
        budget = self.limit - len(ELISION.encode())
        head_budget = budget // 2
        tail_budget = budget - head_budget
        return (
            bytes(self.head[:head_budget]).decode("utf-8", errors="ignore")
            + ELISION
            + (tail[-tail_budget:].decode("utf-8", errors="ignore") if tail_budget else "")
        )


class TerminalScanner:
    """Scan fixed windows, including boundaries within unusually large frames."""

    def __init__(self, marker: str):
        self.window_size = len(marker) + 64
        self.tail = ""
        self.pattern = re.compile(re.escape(marker) + r":exit:(\d{1,10})\s")
        self.returncode: int | None = None
        self.prompt = False

    def scan_window(self, window: str) -> None:
        match = self.pattern.search(window)
        if match and self.returncode is None:
            self.returncode = int(match.group(1))

    def feed(self, text: str) -> None:
        for start in range(0, len(text), self.window_size):
            window = self.tail + text[start : start + self.window_size]
            self.scan_window(window)
            self.tail = window[-self.window_size :]
        self.prompt = bool(re.search(r"[$#]\s*$", self.tail))


def iter_output_file(path: str | os.PathLike[str], *, chunk_size: int = 65536) -> Iterator[str]:
    """Read a UTF-8 capture in at most chunk_size characters per page."""
    from inspire.platform.errors import ValidationError, TransportError

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValidationError("chunk_size must be a positive integer.")
    try:
        with open(path, encoding="utf-8", newline="") as stream:
            while chunk := stream.read(chunk_size):
                yield chunk
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError,
            PermissionError, UnicodeError) as error:
        # Absent, unreadable or undecodable is permanent for this path: the same
        # call cannot start working, so it must not arrive marked retryable.
        raise ValidationError(f"Cannot read output file: {error}") from error
    except OSError as error:
        raise TransportError(f"Cannot read output file: {error}") from error
