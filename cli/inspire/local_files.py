"""Private local state and atomic publication, without application dependencies.

Windows profile ACLs normally exclude other standard users already. The explicit
ACL is defence in depth; it is neither encryption nor protection from administrators.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)
_warned_paths: set[str] = set()
# Cache attempts, including failures: unavailable PowerShell must not turn every
# session save/read into another process launch. Restart to retry after ACL repair.
_windows_directory_attempts: set[str] = set()
_windows_directory_lock = threading.Lock()


# Only paths and the repair flag enter PowerShell; credential contents never
# enter its arguments or diagnostics. ACL repair never reads file contents.
_WINDOWS_ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
# Python may inherit PS7's module path when launching Windows PowerShell.
# Load the security cmdlets from this engine's own installation explicitly.
Import-Module "$PSHOME\Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1"
$path = $env:INSPIRE_KEY_EXPORT_PATH
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$item = Get-Item -LiteralPath $path -Force
$ancestor = $item
while ($null -ne $ancestor) {
    if ($ancestor.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw 'Refusing private permissions through a reparse point'
    }
    $ancestor = if ($ancestor.PSIsContainer) { $ancestor.Parent } else { $ancestor.Directory }
}
if ($env:INSPIRE_PRIVATE_REPAIR -eq '1') {
    $acl = Get-Acl -LiteralPath $path
    # Removing group grants must not remove the caller's own access.
    $ownerRules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) |
        Where-Object { $_.IdentityReference.Value -eq $sid.Value -and
            $_.AccessControlType -eq 'Allow' -and
            ($_.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -eq
                [System.Security.AccessControl.FileSystemRights]::FullControl })
    if ($ownerRules.Count -eq 0) { exit 3 }
    $acl.SetAccessRuleProtection($true, $true)
    foreach ($entry in @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))) {
        if ($entry.AccessControlType -eq 'Allow' -and $entry.IdentityReference.Value -ne $sid.Value) {
            [void]$acl.RemoveAccessRuleSpecific($entry)
        }
    }
    if ($item.PSIsContainer) {
        # Files created here inherit this grant; never repair individual caches.
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $path -AclObject $acl
    $actual = Get-Acl -LiteralPath $path
    if (!$actual.AreAccessRulesProtected) { throw 'Private ACL verification failed' }
    foreach ($entry in @($actual.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))) {
        if ($entry.AccessControlType -eq 'Allow' -and $entry.IdentityReference.Value -ne $sid.Value) {
            throw 'Private ACL verification failed'
        }
    }
    if ($item.PSIsContainer) {
        $inheriting = @($actual.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) |
            Where-Object { $_.IdentityReference.Value -eq $sid.Value -and
                $_.AccessControlType -eq 'Allow' -and
                $_.FileSystemRights -eq 'FullControl' -and
                ($_.InheritanceFlags -band 3) -eq 3 -and $_.PropagationFlags -eq 'None' })
        if ($inheriting.Count -eq 0) { throw 'Private directory inheritance verification failed' }
    }
    return
}
$acl = if ($item.PSIsContainer) {
    New-Object System.Security.AccessControl.DirectorySecurity
} else {
    New-Object System.Security.AccessControl.FileSecurity
}
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true, $false)
$rule = if ($item.PSIsContainer) {
    New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
} else {
    New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
}
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $path -AclObject $acl
$actual = Get-Acl -LiteralPath $path
$rules = @($actual.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
if (!$actual.AreAccessRulesProtected -or $rules.Count -ne 1 -or
    $rules[0].IdentityReference.Value -ne $sid.Value -or
    $rules[0].AccessControlType -ne 'Allow' -or
    $rules[0].FileSystemRights -ne 'FullControl') {
    throw 'Private file ACL verification failed'
}
"""


def windows_acl_tool() -> str:
    tool = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not tool:
        raise ValueError(
            "Private file permissions require PowerShell on Windows (powershell.exe or pwsh.exe)."
        )
    return tool


def restrict_windows_file(path: str, *, tool: str | None = None, repair: bool = False) -> None:
    env = os.environ.copy()
    env["INSPIRE_KEY_EXPORT_PATH"] = os.path.abspath(path)
    env["INSPIRE_PRIVATE_REPAIR"] = "1" if repair else "0"
    try:
        result = subprocess.run(
            [
                tool or windows_acl_tool(),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _WINDOWS_ACL_SCRIPT,
            ],
            env=env,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Could not establish private Windows file permissions.") from None
    if result.returncode == 3:
        raise ValueError(
            "Existing Windows ACL lacks direct current-user FullControl; "
            "left unchanged to avoid removing your access."
        )
    if result.returncode:
        raise ValueError("Could not verify private Windows file permissions.")


def _warn_once(path: Path, reason: str) -> None:
    key = str(path.absolute())
    if key not in _warned_paths:
        _warned_paths.add(key)
        logger.warning("Could not restrict private path %s: %s", path, reason)


def _no_symlinks(path: Path) -> bool:
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return False
    return True


def _restrict_windows_directory(path: Path) -> None:
    """Establish inheriting ACLs once per directory per process, best effort.

    Existing explicit file grants, disabled inheritance, moved-in files and
    hard links are not repaired by a parent ACL. Atomic writes create a new
    inheriting file in this directory; explicit key exports verify their own ACL.
    Cached directories must not be replaced or have their ACL changed externally
    during the process lifetime. Failures are warned once and retried on restart.
    """
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        return
    directory = path if stat.S_ISDIR(info.st_mode) else path.parent
    key = os.path.normcase(str(directory.absolute()))
    with _windows_directory_lock:
        if key in _windows_directory_attempts:
            return
        if not _no_symlinks(directory):
            return
        _windows_directory_attempts.add(key)
        try:
            restrict_windows_file(str(directory), repair=True)
        except (OSError, ValueError) as error:
            _warn_once(directory, str(error))


def prepare_windows_directory(path: Path, *, create: bool = False) -> None:
    """Warm ACLs during path resolution without creating accounts on lookup.

    Storage constructors may request creation before entering their first lock.
    Merely resolving a missing home/account must remain a read-only operation.
    """
    try:
        if create:
            ensure_private_directory(path)
            repair_inspire_path(path)
        elif path.is_dir():
            _restrict_windows_directory(path)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as error:
        _warn_once(path, str(error))


def restrict_private_path(path: Path) -> None:
    """Narrow POSIX permissions or protect a Windows directory once per process."""
    try:
        if sys.platform == "win32":
            _restrict_windows_directory(path)
            return
        if not _no_symlinks(path):
            return
        # Opening every component relative to its parent closes the symlink
        # substitution race between checking a path and changing permissions.
        absolute = path.absolute()
        descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in absolute.parts[1:]:
                child = os.open(
                    part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = child
            info = os.fstat(descriptor)
            if stat.S_ISDIR(info.st_mode):
                allowed = 0o700
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                allowed = 0o600
            else:
                return
            mode = stat.S_IMODE(info.st_mode)
            if mode & ~allowed:
                os.fchmod(descriptor, mode & allowed)
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as error:
        # OS error text cannot contain file contents; ACL subprocess output is
        # deliberately replaced with a fixed reason before it reaches here.
        _warn_once(path, str(error))


def repair_inspire_path(path: Path) -> None:
    """Repair only existing state beneath the user's Inspire home."""
    root = Path.home() / ".inspire"
    try:
        path.absolute().relative_to(root.absolute())
        if ".." in path.parts:
            return
        for parent in reversed(path.absolute().parents):
            if parent == root.absolute() or root.absolute() in parent.parents:
                restrict_private_path(parent)
        restrict_private_path(path)
    except (OSError, ValueError):
        return


def ensure_private_directory(path: Path) -> None:
    if not path.exists():
        if not path.parent.exists():
            ensure_private_directory(path.parent)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    restrict_private_path(path)


def replace_with_retry(source: Path, target: Path) -> None:
    """Allow a short-lived Windows reader to release the destination handle."""
    for attempt in range(5):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def atomic_write_text(target: Path, content: str, *, private: bool = False) -> None:
    """Publish a complete file; Windows temporaries inherit the directory ACL."""
    if private:
        ensure_private_directory(target.parent)
        repair_inspire_path(target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive unique names prevent concurrent writers from sharing a temporary
    # inode, even when the destination has no cooperating lock.
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if private and sys.platform != "win32":
                if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) & ~0o600:
                    raise OSError(
                        "The destination filesystem did not enforce private 0600 permissions."
                    )
                # Replacing an already narrower file must not grant access.
                try:
                    info = target.lstat()
                    if stat.S_ISREG(info.st_mode):
                        os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode) & 0o600)
                except FileNotFoundError:
                    pass
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
