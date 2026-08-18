"""Flags for spawning a background child that outlives the command.

The CLI fires two of these off during a normal invocation — the update check and
the resource-index refresh — and neither should be visible to the user or tied
to the terminal they started from.

``start_new_session=True`` does that on POSIX. On Windows it is accepted and
silently ignored, which leaves the child attached to the parent's console: it
pops a window, and it shares the console's Ctrl-C. Sharing the console group is
the part that bites hardest — a Ctrl-C aimed at anything in that group reaches
every process in it, including the one that spawned the child.
"""

from __future__ import annotations

import subprocess
import sys


def detached_creationflags() -> int:
    """Return Popen ``creationflags`` for a detached child, 0 off Windows.

    Popen rejects a non-zero value on POSIX, which is why this returns 0 there
    and leaves detachment to ``start_new_session``.
    """
    if sys.platform != "win32":
        return 0
    # Guarded lookups: these constants exist only in the Windows stdlib.
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | int(
        getattr(subprocess, "DETACHED_PROCESS", 0)
    )


__all__ = ["detached_creationflags"]
