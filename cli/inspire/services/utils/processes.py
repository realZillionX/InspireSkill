"""Spawning and probing the background work the CLI leaves behind.

The update check should not be visible to the user or tied to the terminal it
started from. Cache refresh leases also record an owning PID so an interrupted
refresh can be recovered without waiting for the lease TTL.

Both of the POSIX idioms for that are quietly wrong on Windows, and in the same
direction: they reach the caller's own console.

- ``start_new_session=True`` is accepted and ignored, leaving the child attached
  to the parent's console, sharing its Ctrl-C.
- ``os.kill(pid, 0)`` is not a liveness probe. Windows has no signal 0, and
  ``signal.CTRL_C_EVENT`` *is* 0, so it sends Ctrl-C to the process's whole
  console group.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys

# GetExitCodeProcess reports _STILL_ACTIVE while a process is running, and
# PROCESS_QUERY_LIMITED_INFORMATION is the least access that can ask for it.
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5


def process_is_alive(pid: int) -> bool:
    """Whether *pid* is still running, without ever signalling it.

    The POSIX ``os.kill(pid, 0)`` probe is a no-op. On Windows the same call is
    a real Ctrl-C event, so query the process handle instead. Access denied is
    treated conservatively as alive: it proves a process occupies the PID even
    though this caller cannot inspect its exit code.
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as exc:
            return exc.errno != errno.ESRCH
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = getattr(ctypes, "windll").kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetLastError.restype = wintypes.DWORD
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return int(kernel32.GetLastError()) == _ERROR_ACCESS_DENIED
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
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
