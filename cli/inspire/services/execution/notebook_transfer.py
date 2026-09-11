"""Path validation, inventories and staged publication for notebook transfers.

The SDK chooses SSH or Jupyter and resolves paths before reaching these helpers.
A destination names the exact file or directory, not a parent to append a basename
to. Jupyter path translation preserves container identity and refuses paths outside
serverRoot; terminal cwd is not evidence of that root.

SSH requires remote python3 and space for a full copy in /tmp. Files publish
atomically, but directory merges have no rollback and do not snapshot a changing
source. Cleanup is best effort within the remaining budget: cancellation or a lost
connection can leave remote staging files. Bridge creation belongs to the CLI.
"""
from __future__ import annotations

from inspire.platform.web.flow import blocking_io, blocking_call, call, perform_sync

import contextlib
import json
from html.parser import HTMLParser
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote

from inspire.bridge.tunnel.config import load_tunnel_config
from inspire.bridge.tunnel.scp import run_scp_transfer
from inspire.services.execution.remote_exec import exec_in_notebook_ssh

DEFAULT_JUPYTER_MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class TransferResult:
    """A completed transfer, with both the requested and resolved remote path.

    SDK remote preserves the caller's spelling; remote_path is the absolute
    container destination on upload or source on download. Use the latter with
    exec because command cwd may differ. bytes_transferred counts file content,
    not base64 or protocol overhead; directories themselves do not count as files.
    This result does not promise a transactional directory snapshot.
    """

    local: str
    remote: str
    bytes_transferred: int
    transport: str
    files_transferred: int = 1
    remote_path: str = field(kw_only=True)


def remote_path(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError("remote must be a non-empty path without control characters.")
    decoded = value
    while True:
        if ".." in decoded.split("/") or "\\" in decoded:
            raise ValueError("Remote path traversal (.. or backslash) is not allowed.")
        following = unquote(decoded)
        if following == decoded:
            break
        decoded = following
    path = str(PurePosixPath(value))
    if path in (".", "/"):
        raise ValueError("remote must name a file or directory, not the root.")
    return path


class _LabConfigParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_config = False
        self.data = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.in_config = dict(attrs).get("id") == "jupyter-config-data"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.in_config = False

    def handle_data(self, data: str) -> None:
        if self.in_config:
            self.data += data


def jupyter_contents_root(page: str) -> str:
    """Read JupyterLab's serverRoot, never infer it from a terminal's cwd."""
    parser = _LabConfigParser()
    parser.feed(page)
    try:
        config = json.loads(parser.data)
    except ValueError:
        config = None
    root = config.get("serverRoot") if isinstance(config, dict) else None
    if (not isinstance(root, str) or not root.startswith("/")
            or root.startswith("//") or ".." in root.split("/")
            or "\\" in root or any(ord(c) < 32 for c in root)):
        raise ValueError(
            'Cannot discover the Jupyter contents root: JupyterLab omitted a valid '
            'absolute serverRoot; use an absolute remote path with transport="ssh".'
        )
    return str(PurePosixPath(root))


def contents_path(remote: str, root: str) -> str:
    """Translate a container path into a contents API path without rebasing it."""
    path = PurePosixPath(remote)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            raise ValueError(
                f'Remote path {remote!r} is outside the Jupyter contents root {root!r}; '
                'use transport="ssh".'
            ) from None
    if str(path) == ".":
        raise ValueError('remote names the Jupyter contents root; use transport="ssh" for directories.')
    return str(path)


def check_size(size: int, cap: int) -> None:
    if size > cap:
        raise ValueError(
            f"File exceeds Jupyter size cap ({cap} bytes); use transport='ssh' "
            "for large files, or deliberately raise max_bytes."
        )


@blocking_io
def inventory(path: Path) -> tuple[int, int]:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Symbolic links are not supported in transfers.")
    if path.is_file():
        return path.stat().st_size, 1
    if not path.is_dir():
        raise ValueError(f"Not a regular file or directory: {path}")
    size = count = 0
    for child in path.iterdir():
        child_size, child_count = inventory(child)
        size += child_size
        count += child_count
    return size, count


@blocking_io
def publish(source: Path, destination: Path, overwrite: bool) -> None:
    """Publish complete files; directory merges are incremental, not transactional."""
    if any(parent.is_symlink() for parent in (destination, *destination.parents)):
        raise ValueError("Destination must not be a symbolic link.")
    if not overwrite and destination.exists():
        raise ValueError(f"Destination already exists: {destination}")
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            publish(child, destination / child.name, overwrite)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".inspire-transfer-", dir=destination.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, temporary)
        if overwrite:
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


# Only this fixed program is executed remotely; arguments are JSON and shell quoted.
_REMOTE = r'''
from __future__ import annotations
import json, os, pathlib, shutil, sys, tempfile
Path = pathlib.Path
'''


def transfer_ssh(
    *, local: str, remote: str, download: bool, recursive: bool,
    overwrite: bool, bridge_name: str, account: str, timeout: float,
) -> TransferResult:
    """Stage via SCP and publish complete files at an already resolved remote path.

    Caller-side path resolution must happen first: this layer does not discover
    Jupyter serverRoot. Directory merges may publish some files before failing.
    The remote helper is built from inventory/publish so both ends enforce the
    same rules; remote python3 and temporary disk space are required.
    """
    import inspect

    deadline = time.monotonic() + timeout
    config = load_tunnel_config(account=account)
    program = _REMOTE + perform_sync(blocking_call(inspect.getsource, inventory)) + "\n" + perform_sync(blocking_call(inspect.getsource, publish))
    program = program.replace("@blocking_io\n", "")
    program += "\na = json.loads(sys.argv[1])\np = Path(a['remote'])\n"
    program += """
if a['action'] == 'prepare':
    if a['download']:
        size, count = inventory(p)
        if p.is_dir() and not a['recursive']:
            raise ValueError('Directories require recursive=True and transport=ssh')
    elif not a['overwrite'] and (p.exists() or p.is_symlink()):
        raise ValueError('Destination already exists: ' + str(p))
    stage = Path(tempfile.mkdtemp(prefix='inspire-transfer-', dir='/tmp'))
    try:
        if a['download']:
            if p.is_dir():
                shutil.copytree(p, stage / 'payload')
            else:
                shutil.copyfile(p, stage / 'payload')
        print(json.dumps({'stage': str(stage)}))
    except BaseException:
        shutil.rmtree(stage)
        raise
elif a['action'] == 'publish':
    # Resolve our own staging directory; /tmp may itself be a system symlink.
    source = Path(a['stage']).resolve() / 'payload'
    size, count = inventory(source)
    publish(source, p, a['overwrite'])
    print(json.dumps({'size': size, 'count': count}))
elif a['action'] == 'cleanup':
    shutil.rmtree(a['stage'])
"""

    def run(action: str, stage: str = "") -> dict[str, Any]:
        args = json.dumps(dict(action=action, stage=stage, remote=remote,
                               download=download, recursive=recursive, overwrite=overwrite))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            from inspire.platform.errors import WaitTimeoutError
            raise WaitTimeoutError("SSH transfer timed out.")
        result = perform_sync(call(exec_in_notebook_ssh,
            bridge_name=bridge_name, account=account,
            command="python3 -c " + shlex.quote(program) + " " + shlex.quote(args),
            timeout=remaining,
        ))
        if not result.completed or result.returncode:
            raise ValueError("SSH transfer failed: " + result.stderr[-1000:])
        return json.loads(result.stdout) if result.stdout.strip() else {}

    stage = str(run("prepare")["stage"])
    if not re.fullmatch(r"/tmp/inspire-transfer-[a-zA-Z0-9_-]+", stage):
        raise ValueError("SSH returned an invalid staging path.")
    try:
        with temporary_directory() as directory:
            target = Path(directory) / "payload" if download else Path(local).absolute()
            result = perform_sync(call(run_scp_transfer,
                local_path=str(target), remote_path=stage + "/payload", download=download,
                recursive=recursive, bridge_name=bridge_name, config=config,
                timeout=max(1, int(deadline - time.monotonic())),
            ))
            if result.returncode:
                raise ValueError("SCP transfer failed: " + (result.stderr or "")[-1000:])
            if download:
                size, count = inventory(target)
                publish(target, Path(local), overwrite)
            else:
                info = run("publish", stage)
                size, count = info["size"], info["count"]
        return TransferResult(local, remote, size, "ssh", count, remote_path=remote)
    finally:
        with contextlib.suppress(Exception):
            run("cleanup", stage)


def contents_url(base: str, path: str) -> str:
    return base + "api/contents/" + quote(path.lstrip("/"), safe="/")


@contextlib.contextmanager
def temporary_directory():
    context = None

    def create():
        nonlocal context
        context = tempfile.TemporaryDirectory(prefix="inspire-transfer-")
        # System temp roots may be symlinks (for example /var on macOS).
        return str(Path(context.name).resolve())

    try:
        yield perform_sync(blocking_call(create))
    finally:
        if context is not None:
            perform_sync(blocking_call(context.cleanup))
