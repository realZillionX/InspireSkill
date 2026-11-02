#!/usr/bin/env python3
"""Optional, stdlib-only evidence log for ``inspire job`` commands.

Usage:
    python job_ops.py [--ledger PATH] create|stop|delete|batch [CLI options]
    python job_ops.py delete run-name --workspace Workspace --account alias
    python job_ops.py batch plan.json --ledger ./job-events.jsonl

This example is not installed as part of the CLI. It finds ``inspire`` on PATH
at each invocation and inherits stdin/stdout/stderr, including delete prompts.
``--ledger`` may also appear among command options, but not after ``--`` or in
another option's value. Use ``--ledger=./-name`` for a dash-prefixed filename.
The default is ~/.inspire/job-events.jsonl, separate from any human JOBS.md;
legacy ledger environment variables are not read.

Each mutation gets a UUID and a durable ``pending`` record before launch, then
``command_exited`` with the actual rc and ``needs_reconcile: true``. These are
command observations, NOT authoritative resource state or proof of success.
Even rc=0 can mean a declined delete or incomplete batch results. Batch records
only the supplied plan path and account; it never reads the plan, captures CLI
output, or claims per-item creation/deletion. Reconcile using live list/status/
events, with the correct account, workspace, filters and pagination, before
retrying. Missing outcomes (including a killed wrapper) also need reconciliation.
Only explicitly supplied account/workspace/project/name/group/quota/pick/nodes
are logged. Commands, environment values, credentials and full argv are omitted.

The small option-arity snapshot is checked against the repo's Click schema by
tests. Unknown options are rejected rather than guessing which tokens are values.
Use the core CLI directly for other commands/options. Help and dry runs never
append. There are no scheduling defaults or account/session-file reads here.

JSONL has no header. Cooperating writers lock the ledger itself (msvcrt on
Windows, fcntl on POSIX), append and fsync without rewriting it. Lock acquisition
waits at most five seconds. Use a local regular file; external rotation, manual
truncation and filesystems without working OS locks are outside this example's
contract. An incomplete final line blocks further appends rather than being
silently repaired. Closing the file releases the lock, including on process exit.

Exit codes: CLI codes pass through (signals become 128+signal at script exit);
2 means wrapper usage error, 127 missing CLI, 126 failure to launch, 73 intent
recording failure (command not started), and 74 outcome recording failure (action
may have succeeded: do not retry blindly). On 74, a best-effort exclusive sibling
<ledger>.<operation UUID>.recovery.jsonl preserves the outcome; if that fails, a
safe JSON record is printed to stderr. No append is retried after an uncertain
write. Recovery may duplicate an already-written outcome; correlate by UUID and
status when reconciling, not by counting records.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import uuid


LOCK_TIMEOUT_SECONDS = 5.0
EXIT_PREFLIGHT = 73
EXIT_RECORDING = 74

# Values mapped to None are consumed but NEVER persisted. These arities/aliases
# come from job_create, job_commands, job_batch and install_account_options.
_COMMON_VALUES: dict[str, str | None] = {"--account": "account"}
_SELECTORS: dict[str, str | None] = {"--workspace": "workspace", "--pick": "pick"}
_VALUE_OPTIONS: dict[str, dict[str, str | None]] = {
    "create": {
        **_COMMON_VALUES,
        "--name": "name",
        "-n": "name",
        "--workspace": "workspace",
        "--project": "project",
        "-p": "project",
        "--group": "group",
        "--quota": "quota",
        "-q": "quota",
        "--nodes": "nodes",
        **dict.fromkeys(
            (
                "--command",
                "-c",
                "--image",
                "-i",
                "--framework",
                "--priority",
                "--fault-tolerance-max-retry",
                "--fault-tolerance-retry-interval",
                "--dataset",
                "--env",
                "--description",
                "--keep-after-success",
                "--keep-after-failure",
                "--max-time",
                "--exclude-node",
                "--specified-node",
                "--shm-size",
            )
        ),
    },
    "stop": {**_COMMON_VALUES, **_SELECTORS},
    "delete": {**_COMMON_VALUES, **_SELECTORS},
    "batch": {**_COMMON_VALUES, "--limit": None, "-n": None},
}
_FLAGS = {
    "create": frozenset(
        (
            "--help",
            "--dry-run",
            "--auto-fault-tolerance",
            "--no-auto-fault-tolerance",
            "--public-path-readonly",
            "--no-public-path-readonly",
            "--enable-notification",
            "--no-enable-notification",
        )
    ),
    "stop": frozenset(("--help",)),
    "delete": frozenset(("--help", "--yes", "-y")),
    "batch": frozenset(("--help", "--dry-run", "--all")),
}


@dataclass
class Invocation:
    action: str | None
    args: list[str]
    ledger: str | None
    metadata: dict[str, str]
    plan_path: str | None = None
    readonly: bool = False


def _take_value(args: Sequence[str], index: int, inline: str | None) -> tuple[str, int]:
    if inline is not None:
        return inline, index + 1
    if index + 1 >= len(args):
        raise ValueError("An option requires a value.")
    return args[index + 1], index + 2


def _take_ledger(args: Sequence[str], index: int) -> tuple[str, int]:
    token = args[index]
    inline = token.partition("=")[2] if "=" in token else None
    value, end = _take_value(args, index, inline)
    if not value.strip() or "\0" in value or (inline is None and value.startswith("-")):
        raise ValueError("--ledger requires a nonempty path (use --ledger=PATH if needed).")
    return value, end


def parse_invocation(argv: Sequence[str]) -> Invocation:
    """Strip only wrapper options, respecting CLI values and the end marker."""
    index = 0
    ledger = None
    while index < len(argv) and (argv[index] == "--ledger" or argv[index].startswith("--ledger=")):
        ledger, index = _take_ledger(argv, index)
    if index == len(argv) or argv[index] == "--help":
        return Invocation(None, [], ledger, {}, readonly=True)
    action = argv[index]
    if action not in _VALUE_OPTIONS:
        raise ValueError("Expected create, stop, delete or batch; use --help for wrapper usage.")
    index += 1
    forwarded: list[str] = []
    metadata: dict[str, str] = {}
    positional: list[str] = []
    readonly = False
    values = _VALUE_OPTIONS[action]
    flags = _FLAGS[action]
    while index < len(argv):
        token = argv[index]
        if token == "--":
            forwarded.extend(argv[index:])
            positional.extend(argv[index + 1 :])
            break
        if token == "--ledger" or token.startswith("--ledger="):
            ledger, index = _take_ledger(argv, index)
            continue
        if token in flags:
            readonly |= token in {"--help", "--dry-run"}
            forwarded.append(token)
            index += 1
            continue
        option, separator, inline = token.partition("=")
        attached = inline if separator and token.startswith("--") else None
        if not token.startswith("--") and len(token) > 2 and token[:2] in values:
            option, attached = token[:2], token[2:]
        if option in values:
            value, end = _take_value(argv, index, attached)
            key = values[option]
            if key is not None:
                metadata[key] = value
            forwarded.extend(argv[index:end])
            index = end
            continue
        if token.startswith("-") and token != "-":
            # Do not echo unknown tokens: they can contain credentials.
            raise ValueError("Unsupported option; check the core CLI help for this command.")
        positional.append(token)
        forwarded.append(token)
        index += 1
    if len(positional) > (0 if action == "create" else 1):
        raise ValueError("Too many positional arguments for this command.")
    plan_path = None
    if positional:
        if action == "batch":
            plan_path = positional[0]
        else:
            metadata["name"] = positional[0]
    return Invocation(action, forwarded, ledger, metadata, plan_path, readonly)


def _acquire_lock(fd: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                # Windows permits locking a byte beyond EOF, including an empty file.
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for the job event ledger lock.") from None
            time.sleep(min(0.05, remaining))


def _encode_event(event: Mapping[str, object]) -> bytes:
    return (json.dumps(dict(event), ensure_ascii=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _write_record(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written == 0:
            raise OSError("Could not finish writing an event record.")
        remaining = remaining[written:]
    os.fsync(fd)


def append_event(path: Path, event: Mapping[str, object], *, timeout: float = 5.0) -> None:
    """Serialize cooperating processes without truncating or rewriting existing data."""
    data = _encode_event(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        path,
        os.O_RDWR
        | os.O_CREAT
        | os.O_APPEND
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0),
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("The ledger must be a regular file.")
        _acquire_lock(fd, timeout)
        if os.fstat(fd).st_size:
            os.lseek(fd, -1, os.SEEK_END)
            if os.read(fd, 1) != b"\n":
                raise OSError("The ledger ends with an incomplete record; reconcile it first.")
        _write_record(fd, data)
    finally:
        os.close(fd)  # Releases the OS lock, even if writing/fsync failed.


def _event(base: Mapping[str, object], status: str, **fields: object) -> dict[str, object]:
    return {
        **base,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status,
        **fields,
    }


def _recover_outcome(path: Path, outcome: Mapping[str, object], error: OSError) -> None:
    """Never retry an uncertain append or overwrite an existing recovery file."""
    recovery = path.with_name(f"{path.name}.{outcome['operation_id']}.recovery.jsonl")
    record = {**outcome, "recording_error": type(error).__name__}
    try:
        fd = os.open(
            recovery, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
        )
        try:
            _write_record(fd, _encode_event(record))
        finally:
            os.close(fd)
    except OSError:
        print(
            "job_ops recovery: " + _encode_event(record).decode("utf-8").rstrip(), file=sys.stderr
        )
    else:
        print("job_ops: recovery record saved beside the ledger (see --help).", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        invocation = parse_invocation(sys.argv[1:] if argv is None else argv)
    except ValueError as exc:
        print(f"job_ops: {exc}", file=sys.stderr)
        return 2
    if invocation.action is None:
        print(__doc__)
        return 0
    executable = shutil.which("inspire")
    if executable is None:
        print(
            "job_ops: inspire executable not found on PATH; command was not started.",
            file=sys.stderr,
        )
        return 127
    command = [executable, "job", invocation.action, *invocation.args]
    path = None
    base: dict[str, object] = {}
    if not invocation.readonly:
        try:
            path = (
                Path(invocation.ledger).expanduser()
                if invocation.ledger is not None
                else Path.home() / ".inspire" / "job-events.jsonl"
            ).resolve()
            base = {
                "schema_version": 1,
                "operation_id": str(uuid.uuid4()),
                "action": invocation.action,
                "scope": "command",
                "metadata": invocation.metadata,
            }
            if invocation.plan_path is not None:
                base["plan_path"] = str(Path(invocation.plan_path).resolve())
            append_event(
                path, _event(base, "pending", needs_reconcile=True), timeout=LOCK_TIMEOUT_SECONDS
            )
        except (OSError, ValueError, RuntimeError) as exc:
            print(
                f"job_ops: cannot record intent ({type(exc).__name__}); command was not started.",
                file=sys.stderr,
            )
            return EXIT_PREFLIGHT
    status = "command_exited"
    needs_reconcile = True
    try:
        rc = subprocess.run(command, check=False).returncode
    except OSError as exc:
        print(f"job_ops: could not launch inspire ({type(exc).__name__}).", file=sys.stderr)
        rc, status, needs_reconcile = 126, "not_started", False
    except KeyboardInterrupt:
        rc, status = 130, "interrupted"
    if path is not None:
        outcome = _event(base, status, rc=rc, needs_reconcile=needs_reconcile)
        try:
            append_event(path, outcome, timeout=LOCK_TIMEOUT_SECONDS)
        except OSError as exc:
            print(
                "job_ops: outcome recording failed; action may have succeeded; "
                "do not retry blindly. Reconcile with live platform state. "
                f"operation_id={base['operation_id']} rc={rc}",
                file=sys.stderr,
            )
            _recover_outcome(path, outcome, exc)
            return EXIT_RECORDING
    return rc


if __name__ == "__main__":
    exit_code = main()
    raise SystemExit(exit_code if exit_code >= 0 else 128 - exit_code)
