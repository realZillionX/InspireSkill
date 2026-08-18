"""Force UTF-8 on the Windows console streams at process start.

Python only routes stdout through the Windows console API — which is UTF-8
internally — when it is attached to a real console. Redirect it to a pipe or a
file and it falls back to the ANSI code page instead, which on a Chinese Windows
is cp936. Every box-drawing rule, every ``›``, and every Chinese workspace name
the CLI prints then raises ``UnicodeEncodeError``.

That is not an edge case here: agent harnesses always invoke the CLI through a
pipe, and ``inspire ... > out.txt`` is how people capture output. Downgrading the
glyphs would fix one symptom on every platform at once; reconfiguring the stream
fixes the cause on the one platform that has it.
"""

from __future__ import annotations

import sys
from typing import IO, Any


def configure_console_encoding(streams: tuple[Any, ...] | None = None) -> None:
    """Reconfigure stdout/stderr to UTF-8 on Windows. No-op elsewhere.

    ``errors="replace"`` is the fuse: a character the terminal genuinely cannot
    render degrades to ``?`` instead of taking down the whole command.
    """
    if sys.platform != "win32":
        return
    for stream in streams if streams is not None else (sys.stdout, sys.stderr):
        _reconfigure_utf8(stream)


def _reconfigure_utf8(stream: IO[str] | Any) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        # Not a TextIOWrapper — a captured buffer under test, or a stream someone
        # replaced. Nothing to do, and nothing worth failing a command over.
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        # Detached or already-closed streams raise here. The command's own
        # output is what matters; a failure to retune the stream is not fatal.
        return


__all__ = ["configure_console_encoding"]
