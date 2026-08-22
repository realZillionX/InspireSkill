"""Spawning the background update-check child the CLI leaves behind.

The update check should not be visible to the user or tied to the terminal they
started from.

Both of the POSIX idioms for that are quietly wrong on Windows, and in the same
direction: they reach the caller's own console.

- ``start_new_session=True`` is accepted and ignored, leaving the child attached
  to the parent's console, sharing its Ctrl-C.
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
