"""Spawning and probing the background children the CLI leaves behind.

The CLI fires two of these off during a normal invocation — the update check and
the resource-index refresh — and neither should be visible to the user or tied
to the terminal they started from.

Both of the POSIX idioms for that are quietly wrong on Windows, and in the same
direction: they reach the caller's own console.

- ``start_new_session=True`` is accepted and ignored, leaving the child attached
  to the parent's console, sharing its Ctrl-C.
- ``os.kill(pid, 0)`` is not a liveness probe. Windows has no signal 0, and
  ``signal.CTRL_C_EVENT`` *is* 0 — so it sends a Ctrl-C to that process's whole
  console group, interrupting the caller and everything else in that terminal.
"""

from __future__ import annotations

import os
import subprocess
import sys

# GetExitCodeProcess reports _STILL_ACTIVE while a process is running, and
# PROCESS_QUERY_LIMITED_INFORMATION is the least access that can ask for it.
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_is_alive(pid: int) -> bool:
    """Whether *pid* is still running. Never signals it.

    ``os.kill(pid, 0)`` is the POSIX idiom and is a no-op probe there. It is not
    portable: Windows has no signal 0, and ``signal.CTRL_C_EVENT`` *is* 0, so
    ``os.kill(pid, 0)`` on Windows sends a Ctrl-C to that process's whole console
    group — which, for a child sharing the caller's console, means interrupting
    the caller and everything else running in that terminal.
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Owned by another user: it exists, which is all this asks.
            return True
        except OSError:
            return False
        return True

    import ctypes

    kernel32 = getattr(ctypes, "windll").kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


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


__all__ = ["detached_creationflags", "process_is_alive"]
