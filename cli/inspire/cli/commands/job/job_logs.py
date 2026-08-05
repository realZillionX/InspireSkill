"""Job logs command."""

from __future__ import annotations

import logging
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Optional

import click

from inspire.bridge.tunnel import (
    TunnelConfig,
    TunnelNotAvailableError,
    _test_ssh_connection,
    is_tunnel_available,
    load_tunnel_config,
    run_ssh_command,
)
from inspire.cli.context import (
    Context,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_JOB_NOT_FOUND,
    EXIT_LOG_NOT_FOUND,
    EXIT_SUCCESS,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import NAME_PICK_HELP
from inspire.cli.utils.job_submit import derive_remote_log_glob
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, WebSession, get_web_session

from .job_commands import (
    WebJobResolutionError,
    WebJobValidationError,
    _close_web_client,
    _reject_web_job_name_at_boundary,
    _reject_job_instance_name,
    _resolve_web_job_id,
    _run_readonly_web_job_operation,
)


logger = logging.getLogger(__name__)

DEFAULT_SSH_TAIL_LINES = 100
DEFAULT_PLATFORM_LOG_RECORDS = 100
DEFAULT_LOG_CHARACTER_LIMIT = 16_000
_FOLLOW_SEEN_LIMIT = 5_000


@dataclass(frozen=True)
class _TextLogSelection:
    content: str
    truncated: bool
    shown: int
    total: int | None
    limit: int | None
    character_limit: int | None


@dataclass(frozen=True)
class _WebLogSelection:
    logs: list[dict[str, Any]]
    truncated: bool
    shown: int
    total: int
    limit: int | None
    character_limit: int | None
    shown_chars: int


def _line_count(text: str) -> int:
    return len(text.splitlines()) if text else 0


def _truncate_text_to_character_limit(
    text: str,
    *,
    character_limit: int,
    keep_tail: bool,
) -> tuple[str, bool]:
    if len(text) <= character_limit:
        return text, False
    if keep_tail:
        return text[-character_limit:], True
    return text[:character_limit], True


def _emit_truncation_hint(
    *,
    shown: int,
    total: int | None,
    unit: str,
    all_output: bool = False,
) -> None:
    if all_output:
        detail = (
            f"showing {shown} of {total} {unit}" if total is not None else f"showing {shown} {unit}"
        )
        click.echo(
            f"Logs incomplete ({detail}); the platform returned fewer records than reported.",
            err=True,
        )
        return
    detail = (
        f"showing {shown} of {total} {unit}" if total is not None else f"showing {shown} {unit}"
    )
    click.echo(f"Logs truncated ({detail}); use --all for complete one-shot output.", err=True)


def _window_to_minutes(window: str) -> int:
    value = (window or "").strip().lower()
    if len(value) < 2:
        raise click.BadParameter("use a window like 30m or 2h")
    unit = value[-1]
    try:
        amount = int(value[:-1])
    except ValueError as exc:
        raise click.BadParameter("use a window like 30m or 2h") from exc
    if amount <= 0:
        raise click.BadParameter("window must be positive")
    if unit == "m":
        return amount
    if unit == "h":
        return amount * 60
    if unit == "d":
        return amount * 24 * 60
    raise click.BadParameter("window unit must be m, h, or d")


def _resolve_latest_log_via_ssh(
    glob_pattern: str, *, bridge_name: Optional[str] = None
) -> Optional[str]:
    """Resolve a log glob pattern to the most recently written matching file.

    Uses ``ls -1t`` so the freshest mtime wins (re-submitting the same job
    NAME picks up the new run, not a clobbered old log). The pattern is
    intentionally NOT shell-quoted so ``*`` expands; the directory and
    sanitized name within it have no other shell metacharacters by
    construction (``sanitize_job_name_for_filename`` strips them).

    Returns the absolute path on hit, ``None`` on no match. Errors during
    SSH propagate up — the caller is the boundary that decides whether to
    surface them.
    """
    cmd = f"ls -1t {glob_pattern} 2>/dev/null | head -n 1"
    try:
        result = run_ssh_command(command=cmd, capture_output=True, bridge_name=bridge_name)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    return out or None


def _coerce_epoch_ms(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _web_log_time_range(job_data: dict, since_minutes: int | None) -> tuple[int, int]:
    now_ms = int(time.time() * 1000)
    if since_minutes is not None:
        return now_ms - since_minutes * 60 * 1000, now_ms

    created_ms = _coerce_epoch_ms(job_data.get("created_at"))
    finished_ms = _coerce_epoch_ms(job_data.get("finished_at"))
    if created_ms is None:
        return now_ms - 24 * 60 * 60 * 1000, now_ms

    start_ms = max(0, created_ms - 10 * 60 * 1000)
    end_ms = (finished_ms or now_ms) + 10 * 60 * 1000
    return start_ms, max(end_ms, start_ms + 1)


def _web_log_sort_key(item: dict) -> tuple[int, str]:
    timestamp_ms = _coerce_epoch_ms(item.get("timestamp_ms")) or 0
    log_id = str(item.get("log_id") or "")
    return timestamp_ms, log_id


def _web_log_identity(item: dict) -> tuple[int, str, str, int]:
    timestamp_ms = _coerce_epoch_ms(item.get("timestamp_ms")) or 0
    log_id = str(item.get("log_id") or "")
    pod_name = str(item.get("pod_name") or "").strip()
    message = str(item.get("message") or item.get("log") or item.get("content") or "")
    return timestamp_ms, log_id, pod_name, hash(message)


def _format_web_log_line(item: dict) -> str:
    timestamp = str(
        item.get("timestamp_str") or item.get("time") or item.get("timestamp_ms") or ""
    ).strip()
    pod_name = scrub_raw_ids(str(item.get("pod_name") or "").strip())
    message = scrub_raw_ids(
        str(item.get("message") or item.get("log") or item.get("content") or "")
    )
    prefix = " ".join(part for part in (timestamp, pod_name) if part)
    return f"{prefix} {message}".rstrip() if prefix else message


def _format_web_logs(logs: list[dict]) -> str:
    if not logs:
        return "No job logs found."

    lines = ["Job Logs"]
    for item in logs:
        lines.append(_format_web_log_line(item))
    return "\n".join(lines)


def _public_web_logs(logs: list[dict]) -> list[dict[str, Any]]:
    public = json_formatter.sanitize_json_data(logs)
    if not isinstance(public, list):
        return []
    return [dict(item) for item in public if isinstance(item, dict)]


def _truncate_web_log_item(
    item: dict[str, Any],
    *,
    character_limit: int,
    keep_tail: bool,
) -> dict[str, Any]:
    rendered = _format_web_log_line(item)
    clipped, _ = _truncate_text_to_character_limit(
        rendered,
        character_limit=character_limit,
        keep_tail=keep_tail,
    )
    if len(rendered) <= character_limit:
        return item

    message_key = next(
        (key for key in ("message", "log", "content") if item.get(key) not in (None, "")),
        None,
    )
    if message_key is None:
        return {"message": clipped}

    truncated = dict(item)
    message = str(truncated.get(message_key) or "")
    without_message = dict(truncated)
    without_message[message_key] = ""
    prefix = _format_web_log_line(without_message).rstrip()
    separator = 1 if prefix and message else 0
    available = character_limit - len(prefix) - separator
    if available <= 0:
        return {"message": clipped}
    shortened, _ = _truncate_text_to_character_limit(
        message,
        character_limit=available,
        keep_tail=keep_tail,
    )
    truncated[message_key] = shortened
    return truncated


def _budget_web_logs(
    logs: list[dict[str, Any]],
    *,
    character_limit: int,
    keep_tail: bool,
) -> tuple[list[dict[str, Any]], bool, int]:
    if not logs:
        return [], False, 0

    candidates = list(reversed(logs)) if keep_tail else list(logs)
    kept: list[dict[str, Any]] = []
    used = 0
    truncated = False

    for item in candidates:
        separator = 1 if kept else 0
        available = character_limit - used - separator
        if available <= 0:
            truncated = True
            break

        rendered = _format_web_log_line(item)
        if len(rendered) > available:
            if not kept:
                kept.append(
                    _truncate_web_log_item(
                        item,
                        character_limit=available,
                        keep_tail=keep_tail,
                    )
                )
                used += len(_format_web_log_line(kept[-1]))
            truncated = True
            break

        kept.append(item)
        used += separator + len(rendered)

    if len(kept) < len(logs):
        truncated = True
    if keep_tail:
        kept.reverse()
    return kept, truncated, used


def _select_web_logs(
    logs: list[dict],
    *,
    total: int,
    tail: int | None,
    head: int | None,
    record_limit: int,
    all_output: bool,
) -> _WebLogSelection:
    public_logs = _public_web_logs(sorted(logs, key=_web_log_sort_key))
    normalized_total = max(int(total), len(public_logs))

    if all_output:
        selected = public_logs
        limit = None
        keep_tail = False
    elif head is not None:
        selected = public_logs[:head]
        limit = head
        keep_tail = False
    elif tail is not None:
        selected = public_logs[-tail:]
        limit = tail
        keep_tail = True
    else:
        selected = public_logs[-record_limit:]
        limit = record_limit
        keep_tail = True

    record_truncated = len(selected) < normalized_total
    if all_output:
        budgeted = selected
        character_truncated = False
        shown_chars = sum(len(_format_web_log_line(item)) for item in budgeted)
        character_limit = None
    else:
        budgeted, character_truncated, shown_chars = _budget_web_logs(
            selected,
            character_limit=DEFAULT_LOG_CHARACTER_LIMIT,
            keep_tail=keep_tail,
        )
        character_limit = DEFAULT_LOG_CHARACTER_LIMIT

    return _WebLogSelection(
        logs=budgeted,
        truncated=record_truncated or character_truncated,
        shown=len(budgeted),
        total=normalized_total,
        limit=limit,
        character_limit=character_limit,
        shown_chars=shown_chars,
    )


def _follow_logs_via_web(
    *,
    job_id: str,
    pod_names: list[str],
    start_ms: int,
    tail_lines: int,
    page_size: int,
    session: WebSession,
    poll_interval: float = 2.0,
) -> None:
    seen: set[tuple[int, str, str, int]] = set()
    seen_order: deque[tuple[int, str, str, int]] = deque()
    first_fetch = True
    truncation_announced = False

    def remember(item: dict) -> None:
        identity = _web_log_identity(item)
        if identity in seen:
            return
        seen.add(identity)
        seen_order.append(identity)
        if len(seen_order) > _FOLLOW_SEEN_LIMIT:
            seen.discard(seen_order.popleft())

    try:
        while True:
            end_ms = int(time.time() * 1000)
            logs, _total = browser_api_module.list_train_job_logs(
                job_id=job_id,
                pod_names=pod_names,
                start_timestamp_ms=start_ms,
                end_timestamp_ms=end_ms,
                page_size=max(page_size, tail_lines),
                session=session,
            )
            ordered = sorted(logs, key=_web_log_sort_key)
            unseen = [item for item in ordered if _web_log_identity(item) not in seen]

            if first_fetch:
                unseen = unseen[-tail_lines:]
                first_fetch = False

            public_unseen = _public_web_logs(unseen)
            shown, truncated, _shown_chars = _budget_web_logs(
                public_unseen,
                character_limit=DEFAULT_LOG_CHARACTER_LIMIT,
                keep_tail=True,
            )
            for item in shown:
                click.echo(_format_web_log_line(item))
            if truncated and not truncation_announced:
                click.echo(
                    "Follow update truncated to the character budget; long or excess records were skipped.",
                    err=True,
                )
                truncation_announced = True

            for item in ordered:
                remember(item)

            time.sleep(poll_interval)
    except KeyboardInterrupt:
        return


def _fetch_log_via_ssh(
    remote_log_path: str,
    tail: Optional[int] = None,
    head: Optional[int] = None,
    bridge_name: Optional[str] = None,
) -> _TextLogSelection:
    all_output = tail is None and head is None
    if tail is not None:
        line_limit = tail
        command = f"tail -n {line_limit + 1} '{remote_log_path}'"
        keep_tail = True
    elif head is not None:
        line_limit = head
        command = f"head -n {line_limit + 1} '{remote_log_path}'"
        keep_tail = False
    else:
        line_limit = None
        command = f"cat '{remote_log_path}'"
        keep_tail = False

    try:
        result = run_ssh_command(command=command, capture_output=True, bridge_name=bridge_name)
    except Exception as exc:
        logger.debug(
            "SSH job log read failed for path %r: %s",
            remote_log_path,
            exc,
            exc_info=True,
        )
        raise IOError("Could not read the requested job log over SSH.") from None

    if result.returncode != 0:
        logger.debug(
            "SSH job log read failed for path %r: %s",
            remote_log_path,
            str(result.stderr or "").strip(),
        )
        raise IOError("Could not read the requested job log over SSH.")

    content = scrub_raw_ids(result.stdout or "")
    raw_lines = content.splitlines(keepends=True)
    line_truncated = False
    if line_limit is not None and len(raw_lines) > line_limit:
        line_truncated = True
        raw_lines = raw_lines[-line_limit:] if keep_tail else raw_lines[:line_limit]
    selected = "".join(raw_lines)

    if all_output:
        character_truncated = False
        character_limit = None
    else:
        selected, character_truncated = _truncate_text_to_character_limit(
            selected,
            character_limit=DEFAULT_LOG_CHARACTER_LIMIT,
            keep_tail=keep_tail,
        )
        character_limit = DEFAULT_LOG_CHARACTER_LIMIT

    return _TextLogSelection(
        content=selected,
        truncated=line_truncated or character_truncated,
        shown=_line_count(selected),
        total=_line_count(content) if all_output else None,
        limit=line_limit,
        character_limit=character_limit,
    )


def _follow_logs_via_ssh(
    job_id: str,
    remote_log_path: str,
    tail_lines: int = 50,
    wait_timeout: int = 300,
    bridge_name: Optional[str] = None,
    status_hint: str = "inspire job status <job-name> --workspace <workspace>",
) -> Optional[str]:
    import select
    import subprocess
    import time

    from inspire.bridge.tunnel import get_ssh_command_args

    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level
    api_logger.setLevel(logging.CRITICAL)

    session = get_web_session()
    terminal_statuses = {
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "job_succeeded",
        "job_failed",
        "job_cancelled",
    }
    final_status = None
    status_check_interval = 5

    is_glob = "*" in remote_log_path
    start_time = time.time()
    concrete_log_path: Optional[str] = None

    while time.time() - start_time < wait_timeout:
        try:
            if is_glob:
                # Re-resolve every iteration so a fresh log file (post-create
                # eventual consistency on the FS, or job_id-based suffix that
                # only materializes when the job's container starts) is
                # picked up automatically.
                resolved = _resolve_latest_log_via_ssh(remote_log_path, bridge_name=bridge_name)
                if resolved:
                    concrete_log_path = resolved
                    break
            else:
                check_cmd = f"test -f '{remote_log_path}' && echo 'exists' || echo 'waiting'"
                result = run_ssh_command(check_cmd, timeout=10, bridge_name=bridge_name)
                if "exists" in result.stdout:
                    concrete_log_path = remote_log_path
                    break
        except Exception:
            pass

        time.sleep(5)

    if not concrete_log_path:
        click.echo(f"\n\nTimeout: Log file not created after {wait_timeout}s")
        click.echo(f"Job may still be queuing. Check status with: {status_hint}")
        return None

    # Past the wait gate — switch to the concrete path for tail -f.
    remote_log_path = concrete_log_path

    command = f"tail -n {tail_lines} -f '{remote_log_path}'"
    ssh_args = get_ssh_command_args(bridge_name=bridge_name, remote_command=command)

    process = None
    truncation_announced = False

    def emit_follow_line(line: str) -> None:
        nonlocal truncation_announced
        sanitized = scrub_raw_ids(line)
        bounded, truncated = _truncate_text_to_character_limit(
            sanitized,
            character_limit=DEFAULT_LOG_CHARACTER_LIMIT,
            keep_tail=False,
        )
        click.echo(bounded, nl=False)
        if truncated and not truncation_announced:
            if bounded and not bounded.endswith("\n"):
                click.echo()
            click.echo(
                "Follow line truncated to the character budget.",
                err=True,
            )
            truncation_announced = True

    try:
        process = subprocess.Popen(
            ssh_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )
        stdout = process.stdout
        if stdout is None:
            raise RuntimeError("SSH process stdout pipe was not created")

        last_status_check = time.time()

        while True:
            if process.poll() is not None:
                for line in stdout:
                    emit_follow_line(line)
                break

            ready, _, _ = select.select([stdout], [], [], 1.0)

            if ready:
                line = stdout.readline()
                if line:
                    emit_follow_line(line)
                elif process.poll() is not None:
                    break

            current_time = time.time()
            if current_time - last_status_check >= status_check_interval:
                last_status_check = current_time
                try:
                    job_data = browser_api_module.get_job_detail_v2(job_id, session=session)
                    current_status = job_data.get("status", "UNKNOWN")

                    if current_status in terminal_statuses:
                        final_status = current_status
                        time.sleep(3)
                        stdout.close()
                        break
                except Exception:
                    pass

    except KeyboardInterrupt:
        return final_status
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()
        api_logger.setLevel(original_level)

    return final_status


def _find_connected_tunnel_bridges(
    *,
    exclude: Optional[str] = None,
    timeout: int = 5,
) -> list[str]:
    """Best-effort probe for connected tunnel profiles."""
    try:
        config = load_tunnel_config()
    except Exception:
        return []

    excluded = (exclude or "").strip()
    candidates = [bridge for bridge in config.list_bridges() if bridge.name != excluded]
    if not candidates:
        return []

    connected: list[str] = []
    max_workers = min(len(candidates), 8)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_test_ssh_connection, bridge, config, timeout): bridge.name
            for bridge in candidates
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                if future.result():
                    connected.append(name)
            except Exception:
                continue

    return sorted(connected)


def _resolve_tunnel_preflight_target(
    bridge_name: Optional[str],
) -> tuple[Optional[str], TunnelConfig | None, bool]:
    """Resolve the bridge/config tuple used by SSH availability preflight."""
    try:
        tunnel_config = load_tunnel_config()
    except Exception:
        return bridge_name, None, bool(bridge_name)

    if bridge_name:
        return bridge_name, tunnel_config, tunnel_config.get_bridge(bridge_name) is not None

    bridge = tunnel_config.get_bridge()
    if bridge is None:
        return None, tunnel_config, False

    return bridge.name, tunnel_config, True


def _emit_no_tunnel_error(ctx: Context, *, bridge_name: Optional[str]) -> None:
    connected = _find_connected_tunnel_bridges(exclude=bridge_name)
    if connected:
        preview = ", ".join(connected[:5])
        if len(connected) > 5:
            preview = f"{preview}, +{len(connected) - 5} more"
        hint = (
            f"Cached notebook tunnel(s): {preview}. "
            "Pass --notebook <name> to target one explicitly, "
            "or run `inspire notebook connection refresh <notebook-name> --workspace <workspace>` to bootstrap a new one."
        )
    else:
        hint = (
            "No cached notebook tunnels found. "
            "Run `inspire notebook connection refresh <notebook-name> --workspace <workspace>` to bootstrap one with shared-FS access."
        )
    label = f"bridge '{bridge_name}'" if bridge_name else "default bridge"
    _handle_error(
        ctx,
        "TunnelError",
        f"SSH tunnel not available for {label}.",
        EXIT_GENERAL_ERROR,
        hint=hint,
    )


def _run_job_logs_single_job(
    ctx: Context,
    *,
    job: str,
    job_id: str,
    remote_log_path: str,
    tail: int | None,
    head: int | None,
    path: bool,
    follow: bool,
    all_output: bool,
    workspace: Optional[str],
    bridge_name: Optional[str] = None,
) -> None:
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)

        effective_bridge_name, tunnel_config, bridge_configured = _resolve_tunnel_preflight_target(
            bridge_name
        )
        bridge_name_for_checks = effective_bridge_name or bridge_name

        tunnel_ok = is_tunnel_available(
            bridge_name=bridge_name_for_checks,
            config=tunnel_config,
            retries=0,
            retry_pause=0.0,
            progressive=False,
        )
        if not tunnel_ok:
            if bridge_name and tunnel_config is not None and not bridge_configured:
                _handle_error(
                    ctx,
                    "NotebookTunnelNotFound",
                    f"No cached notebook tunnel for '{bridge_name}'.",
                    EXIT_GENERAL_ERROR,
                    hint="Run `inspire notebook connection refresh <name> --workspace <workspace>` to create or refresh a notebook connection.",
                )
            _emit_no_tunnel_error(ctx, bridge_name=bridge_name)
            return

        if path:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json(
                        {
                            "name": scrub_raw_ids(job),
                            "status": "log-location-selected",
                        }
                    )
                )
            else:
                click.echo(f"Log location selected for {scrub_raw_ids(job)}.")
            sys.exit(EXIT_SUCCESS)

        if follow:
            if ctx.json_output:
                _handle_error(
                    ctx,
                    "InvalidUsage",
                    "--follow requires the human-readable output mode.",
                    EXIT_GENERAL_ERROR,
                    hint="Drop --json or use a one-shot fetch (default / --tail / --head).",
                )
                return

            final_status = _follow_logs_via_ssh(
                job_id=job_id,
                remote_log_path=remote_log_path,
                tail_lines=tail or DEFAULT_SSH_TAIL_LINES,
                bridge_name=bridge_name,
                status_hint=f"inspire job status {job} --workspace {workspace or '<workspace>'}",
            )

            if final_status in {"SUCCEEDED", "job_succeeded"}:
                sys.exit(EXIT_SUCCESS)
            if final_status in {"FAILED", "CANCELLED", "job_failed", "job_cancelled"}:
                sys.exit(EXIT_GENERAL_ERROR)
            sys.exit(EXIT_SUCCESS)

        try:
            selection = _fetch_log_via_ssh(
                remote_log_path=remote_log_path,
                tail=tail
                if tail is not None
                else (None if all_output or head else DEFAULT_SSH_TAIL_LINES),
                head=head,
                bridge_name=bridge_name,
            )
        except IOError as e:
            _handle_error(
                ctx,
                "LogNotFound",
                str(e),
                EXIT_LOG_NOT_FOUND,
                hint=(
                    "If the job hasn't started yet the log file may not exist. "
                    f"Check `inspire job status {job} --workspace {workspace or '<workspace>'}` "
                    "and try again, or pass --remote-log-path if the path differs from the "
                    "default training_master_<name>.log convention."
                ),
            )
            return

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "content": selection.content,
                        "truncated": selection.truncated,
                        "shown": selection.shown,
                        "total": selection.total,
                        "limit": selection.limit,
                        "character_limit": selection.character_limit,
                    }
                )
            )
            return

        click.echo(selection.content)
        if selection.truncated:
            _emit_truncation_hint(
                shown=selection.shown,
                total=selection.total,
                unit="lines",
                all_output=all_output,
            )

    except TunnelNotAvailableError:
        _emit_no_tunnel_error(ctx, bridge_name=bridge_name)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


def _run_job_logs_web_single_job(
    ctx: Context,
    *,
    job: str,
    tail: int | None,
    head: int | None,
    path: bool,
    follow: bool,
    all_output: bool,
    workspace: Optional[str],
    all_workspaces: bool,
    max_pages: int,
    pick: int | None,
    instance_names: tuple[str, ...],
    since_minutes: int | None,
    web_page_size: int,
) -> None:
    if path:
        _handle_error(
            ctx,
            "InvalidUsage",
            "--path is not supported for web aggregated logs",
            EXIT_VALIDATION_ERROR,
        )
        return
    if follow and ctx.json_output:
        _handle_error(
            ctx,
            "InvalidUsage",
            "--json --follow --source platform is not supported",
            EXIT_VALIDATION_ERROR,
            hint="Drop --json to follow platform logs, or drop --follow for a one-shot JSON fetch.",
        )
        return
    if since_minutes is not None and since_minutes <= 0:
        _handle_error(
            ctx,
            "InvalidUsage",
            "--window must be positive",
            EXIT_VALIDATION_ERROR,
        )
        return
    if web_page_size <= 0:
        _handle_error(
            ctx,
            "InvalidUsage",
            "--limit must be positive",
            EXIT_VALIDATION_ERROR,
        )
        return

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        fetch_size = max(web_page_size, tail or 0, head or 0)

        def _load_logs(job_id: str, session: WebSession):
            job_data = browser_api_module.get_job_detail_v2(job_id, session=session)
            if instance_names:
                pod_names = list(instance_names)
            else:
                instances, _ = browser_api_module.list_job_instances(
                    job_id,
                    limit=200,
                    session=session,
                )
                pod_names = [
                    str(item.get("name") or "").strip()
                    for item in instances
                    if str(item.get("name") or "").strip()
                ]

            start_ms, end_ms = _web_log_time_range(job_data, since_minutes)
            if follow or not pod_names:
                return job_id, session, pod_names, start_ms, [], 0

            initial_fetch_size = DEFAULT_PLATFORM_LOG_RECORDS if all_output else fetch_size
            logs, total = browser_api_module.list_train_job_logs(
                job_id=job_id,
                pod_names=pod_names,
                start_timestamp_ms=start_ms,
                end_timestamp_ms=end_ms,
                page_size=initial_fetch_size,
                session=session,
            )
            if all_output and total > len(logs):
                logs, total = browser_api_module.list_train_job_logs(
                    job_id=job_id,
                    pod_names=pod_names,
                    start_timestamp_ms=start_ms,
                    end_timestamp_ms=end_ms,
                    page_size=total,
                    session=session,
                )
            return job_id, session, pod_names, start_ms, logs, total

        try:
            job_id, session, pod_names, start_ms, logs, total = _run_readonly_web_job_operation(
                job=job,
                workspace=workspace,
                all_workspaces=all_workspaces,
                max_pages=max_pages,
                pick=pick,
                workspace_must_be_single=True,
                session_factory=get_web_session,
                resolver=_resolve_web_job_id,
                operation=_load_logs,
            )

            if not pod_names:
                _handle_error(
                    ctx,
                    "LogNotFound",
                    f"No instances found for web job {job}",
                    EXIT_LOG_NOT_FOUND,
                )
                return

            if follow:
                initial_tail = tail or min(
                    web_page_size,
                    DEFAULT_PLATFORM_LOG_RECORDS,
                )
                _follow_logs_via_web(
                    job_id=job_id,
                    pod_names=pod_names,
                    start_ms=start_ms,
                    tail_lines=initial_tail,
                    page_size=web_page_size,
                    session=session,
                )
                return
        finally:
            _close_web_client()

        selection = _select_web_logs(
            logs,
            total=total,
            tail=tail,
            head=head,
            record_limit=web_page_size,
            all_output=all_output,
        )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "logs": selection.logs,
                        "truncated": selection.truncated,
                        "shown": selection.shown,
                        "total": selection.total,
                        "limit": selection.limit,
                        "character_limit": selection.character_limit,
                        "shown_chars": selection.shown_chars,
                    }
                )
            )
            return

        click.echo(_format_web_logs(selection.logs))
        if selection.truncated:
            _emit_truncation_hint(
                shown=selection.shown,
                total=selection.total,
                unit="records",
                all_output=all_output,
            )

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except WebJobResolutionError as e:
        _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except ValueError as e:
        _handle_error(ctx, "APIError", str(e), EXIT_GENERAL_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


@click.command("logs")
@click.argument("job", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option(
    "--tail",
    type=click.IntRange(1),
    help=(
        f"Show the last N lines or platform records. "
        f"Default one-shot output uses {DEFAULT_SSH_TAIL_LINES}."
    ),
)
@click.option("--head", type=click.IntRange(1), help="Show the first N lines or records.")
@click.option(
    "--all",
    "all_output",
    is_flag=True,
    help=(
        f"Show complete one-shot logs without line, record, or the default "
        f"{DEFAULT_LOG_CHARACTER_LIMIT}-character limit. "
        "Cannot be combined with --tail, --head, --limit, --follow, or --path."
    ),
)
@click.option(
    "--path",
    is_flag=True,
    help="Confirm the selected SSH log location without printing the remote path.",
)
@click.option(
    "--follow",
    "-f",
    is_flag=True,
    help=(
        "Follow new log content; platform logs are polled and SSH logs use tail -f. "
        "Initial output is bounded and long updates are shortened."
    ),
)
@click.option(
    "--remote-log-path",
    default=None,
    metavar="PATH",
    help=(
        "Explicit remote log file path. Overrides the default project log path. "
        "Useful for jobs created outside the CLI where the path was set by the platform."
    ),
)
@click.option(
    "--notebook",
    metavar="NAME",
    help=(
        "Notebook name whose cached SSH connection should be used. Required when more "
        "than one is cached and the default connection is ambiguous. "
        "Run `inspire notebook connection refresh <notebook-name> --workspace <workspace>` first."
    ),
)
@click.option(
    "--source",
    type=click.Choice(["platform", "ssh"], case_sensitive=False),
    default="platform",
    show_default=True,
    help=(
        "Log source. Use platform for aggregated platform logs or ssh for "
        "CLI-managed remote log files."
    ),
)
@click.option(
    "--instance",
    "instance_names",
    multiple=True,
    metavar="NAME",
    help="Instance name to query. Repeat for multiple instances.",
)
@click.option(
    "--window",
    default=None,
    help="Relative time window for platform logs, e.g. 30m or 2h.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help=(
        f"Maximum platform records fetched per request (default: "
        f"{DEFAULT_PLATFORM_LOG_RECORDS}). Use --tail for SSH line count."
    ),
)
@pass_context
def logs(
    ctx: Context,
    job: str,
    tail: int | None,
    head: int | None,
    all_output: bool,
    path: bool,
    follow: bool,
    remote_log_path: Optional[str],
    notebook: Optional[str],
    source: str,
    workspace: Optional[str],
    pick: int | None,
    instance_names: tuple[str, ...],
    window: str | None,
    limit: int | None,
) -> None:
    """Read training-job logs from the platform or an SSH log file.

    Platform logs are the default. One-shot output shows a bounded latest
    snapshot and applies a total character budget. Use ``--all`` only when the
    complete log is required. Use ``--source ssh`` for the CLI-managed remote
    log file through a cached notebook bridge.

    \b
    Examples:
        inspire job logs my-training-run --workspace 分布式训练空间
        inspire job logs my-training-run --workspace 分布式训练空间 --tail 100
        inspire job logs my-training-run --workspace 分布式训练空间 --window 30m
        inspire job logs my-training-run --workspace 分布式训练空间 --follow
        inspire job logs my-training-run --workspace 分布式训练空间 --all
        inspire job logs my-training-run --workspace 分布式训练空间 --source ssh --notebook my-cpu-box
    """
    job = _reject_web_job_name_at_boundary(ctx, job)
    if tail is not None and head is not None:
        _handle_error(
            ctx,
            "InvalidUsage",
            "--tail and --head cannot be used together.",
            EXIT_VALIDATION_ERROR,
        )
        return

    all_conflicts = [
        option
        for option, enabled in (
            ("--tail", tail is not None),
            ("--head", head is not None),
            ("--limit", limit is not None),
            ("--follow", follow),
            ("--path", path),
        )
        if enabled
    ]
    if all_output and all_conflicts:
        _handle_error(
            ctx,
            "InvalidUsage",
            f"--all cannot be combined with {', '.join(all_conflicts)}.",
            EXIT_VALIDATION_ERROR,
        )
        return

    if follow and head is not None:
        _handle_error(
            ctx,
            "InvalidUsage",
            "--follow cannot be combined with --head; use --tail for the initial snapshot.",
            EXIT_VALIDATION_ERROR,
        )
        return

    effective_limit = limit or DEFAULT_PLATFORM_LOG_RECORDS

    if notebook is not None:
        from inspire.cli.utils.id_resolver import reject_id_at_boundary

        notebook = reject_id_at_boundary(
            ctx,
            notebook,
            resource_type="notebook",
            list_command="inspire notebook list",
        )
    bridge = notebook
    if instance_names:
        instance_names = tuple(_reject_job_instance_name(ctx, value) for value in instance_names)

    try:
        since_minutes = _window_to_minutes(window) if window else None
    except click.BadParameter as exc:
        _handle_error(ctx, "ValidationError", str(exc), EXIT_VALIDATION_ERROR)
        return

    if source.lower() == "platform":
        if bridge:
            _handle_error(
                ctx,
                "InvalidUsage",
                "--notebook cannot be combined with --source platform",
                EXIT_VALIDATION_ERROR,
            )
            return
        if remote_log_path:
            _handle_error(
                ctx,
                "InvalidUsage",
                "--remote-log-path cannot be combined with --source platform",
                EXIT_VALIDATION_ERROR,
            )
            return
        _run_job_logs_web_single_job(
            ctx,
            job=job,
            tail=tail,
            head=head,
            path=path,
            follow=follow,
            all_output=all_output,
            workspace=workspace,
            all_workspaces=False,
            max_pages=50,
            pick=pick,
            instance_names=instance_names,
            since_minutes=since_minutes,
            web_page_size=effective_limit,
        )
        return

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        if follow:
            try:
                job_id = _run_readonly_web_job_operation(
                    job=job,
                    workspace=workspace,
                    pick=pick,
                    workspace_must_be_single=True,
                    session_factory=get_web_session,
                    resolver=_resolve_web_job_id,
                    operation=lambda resolved_id, session: (
                        browser_api_module.get_job_detail_v2(
                            resolved_id,
                            session=session,
                        ),
                        resolved_id,
                    )[1],
                )
            finally:
                _close_web_client()
        else:
            job_id = _resolve_web_job_id(
                job=job,
                workspace=workspace,
                all_workspaces=False,
                max_pages=50,
                pick=pick,
                workspace_must_be_single=True,
            )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except WebJobValidationError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return
    except WebJobResolutionError as e:
        _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
        return
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
        return

    resolved_log_path: str
    if remote_log_path:
        resolved_log_path = remote_log_path.strip()
        if not resolved_log_path:
            _handle_error(
                ctx,
                "InvalidUsage",
                "--remote-log-path cannot be empty",
                EXIT_GENERAL_ERROR,
            )
            return
    else:
        glob_pattern = derive_remote_log_glob(config, name=job)
        if not glob_pattern:
            _handle_error(
                ctx,
                "ConfigError",
                "Cannot derive remote log path: no default path alias is configured.",
                EXIT_CONFIG_ERROR,
                hint=(
                    "Run `inspire init` to populate the `me` path alias, "
                    "or pass --remote-log-path explicitly."
                ),
            )
            return
        resolved_candidate = _resolve_latest_log_via_ssh(glob_pattern, bridge_name=bridge)
        if resolved_candidate:
            resolved_log_path = resolved_candidate
        else:
            if follow:
                # `_follow_logs_via_ssh` polls for the file's existence with
                # its own wait loop — pass the glob pattern through so it
                # can poll-resolve on each iteration.
                resolved_log_path = glob_pattern
            else:
                _handle_error(
                    ctx,
                    "LogNotFound",
                    "No job log was found on the shared filesystem.",
                    EXIT_LOG_NOT_FOUND,
                    hint=(
                        "The job may not have started writing yet. Pass --follow "
                        "to wait, or pass --remote-log-path if the job uses a "
                        "non-default path (e.g. created outside the CLI)."
                    ),
                )
                return

    if not job_id:
        _handle_error(ctx, "JobNotFound", f"Job not found: {job}", EXIT_JOB_NOT_FOUND)
        return

    _run_job_logs_single_job(
        ctx,
        job=job,
        job_id=job_id,
        remote_log_path=resolved_log_path,
        tail=tail,
        head=head,
        path=path,
        follow=follow,
        all_output=all_output,
        workspace=workspace,
        bridge_name=bridge,
    )


__all__ = ["logs"]
