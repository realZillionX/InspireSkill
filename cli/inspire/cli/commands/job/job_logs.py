"""Job logs command over SSH.

`inspire job logs <name>` cats / tails / follows the remote log file via
a cached notebook connection (any one with shared-FS access; pick explicitly
with ``--notebook``). The log path uses the convention
``<remote_cwd>/.inspire/training_master_<sanitized_name>_<timestamp>.log``;
``inspire job logs`` resolves the latest match via SSH ``ls -1t`` so a
re-submitted name shows the most recent run, not a clobbered file. Pass
``--remote-log-path`` to override (e.g. for jobs created outside the CLI).

Bulk mode and the legacy GitHub-workflow fallback were removed. Use
``inspire notebook ssh <name>`` once to create the cached notebook connection.
"""

from __future__ import annotations

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

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
from inspire.cli.utils.auth import AuthManager
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.job_cli import resolve_job_id
from inspire.cli.utils.job_submit import derive_remote_log_glob
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, WebSession, get_web_session

from .job_commands import WebJobResolutionError, _close_web_client, _resolve_web_job_id


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


def _web_log_identity(item: dict) -> tuple[int, str, str, str]:
    timestamp_ms = _coerce_epoch_ms(item.get("timestamp_ms")) or 0
    log_id = str(item.get("log_id") or "")
    pod_name = str(item.get("pod_name") or "").strip()
    message = str(item.get("message") or item.get("log") or item.get("content") or "")
    return timestamp_ms, log_id, pod_name, message


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
        return "No web logs found."

    lines = ["Web Job Logs"]
    for item in logs:
        lines.append(_format_web_log_line(item))
    return "\n".join(lines)


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
    seen: set[tuple[int, str, str, str]] = set()
    first_fetch = True

    click.echo("Following web logs...")
    click.echo(f"(showing last {tail_lines} lines, then polling new content)")
    click.echo("Press Ctrl+C to stop\n")

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

            for item in unseen:
                click.echo(_format_web_log_line(item))
                seen.add(_web_log_identity(item))

            for item in ordered:
                seen.add(_web_log_identity(item))

            time.sleep(poll_interval)
    except KeyboardInterrupt:
        click.echo("\nStopped following logs.")


def _fetch_log_via_ssh(
    remote_log_path: str,
    tail: Optional[int] = None,
    head: Optional[int] = None,
    bridge_name: Optional[str] = None,
) -> str:
    if tail:
        command = f"tail -n {tail} '{remote_log_path}'"
    elif head:
        command = f"head -n {head} '{remote_log_path}'"
    else:
        command = f"cat '{remote_log_path}'"

    result = run_ssh_command(command=command, capture_output=True, bridge_name=bridge_name)

    if result.returncode != 0:
        raise IOError(f"Failed to read log file: {result.stderr}")

    return result.stdout


def _follow_logs_via_ssh(
    job_id: str,
    config: Config,
    remote_log_path: str,
    tail_lines: int = 50,
    wait_timeout: int = 300,
    bridge_name: Optional[str] = None,
) -> Optional[str]:
    import select
    import subprocess
    import time

    from inspire.bridge.tunnel import get_ssh_command_args

    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level
    api_logger.setLevel(logging.CRITICAL)

    api = AuthManager.get_api(config)
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
    if is_glob:
        click.echo(f"Log file pattern: {scrub_raw_ids(remote_log_path)}")
    else:
        click.echo(f"Log file: {scrub_raw_ids(remote_log_path)}")

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

        elapsed = int(time.time() - start_time)
        click.echo(f"\rWaiting for job to start... ({elapsed}s)", nl=False)
        time.sleep(5)

    if not concrete_log_path:
        click.echo(f"\n\nTimeout: Log file not created after {wait_timeout}s")
        click.echo("Job may still be queuing. Check status with: inspire job status <job-name>")
        return None

    # Past the wait gate — switch to the concrete path for tail -f.
    remote_log_path = concrete_log_path

    click.echo("\nJob started! Following logs...")
    click.echo(f"(showing last {tail_lines} lines, then following new content)")
    click.echo("Press Ctrl+C to stop\n")

    command = f"tail -n {tail_lines} -f '{remote_log_path}'"
    ssh_args = get_ssh_command_args(bridge_name=bridge_name, remote_command=command)

    process = None
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
                    click.echo(scrub_raw_ids(line), nl=False)
                break

            ready, _, _ = select.select([stdout], [], [], 1.0)

            if ready:
                line = stdout.readline()
                if line:
                    click.echo(scrub_raw_ids(line), nl=False)
                elif process.poll() is not None:
                    break

            current_time = time.time()
            if current_time - last_status_check >= status_check_interval:
                last_status_check = current_time
                try:
                    status_result = api.get_job_detail(job_id)
                    job_data = status_result.get("data", {})
                    current_status = job_data.get("status", "UNKNOWN")

                    if current_status in terminal_statuses:
                        final_status = current_status
                        time.sleep(3)
                        stdout.close()
                        break
                except Exception:
                    pass

        if final_status:
            click.echo(f"\n\nJob completed with status: {scrub_raw_ids(final_status)}")

    except KeyboardInterrupt:
        click.echo("\n\nStopped following logs.")
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
            "or run `inspire notebook ssh <notebook-name>` to bootstrap a new one."
        )
    else:
        hint = (
            "No cached notebook tunnels found. "
            "Run `inspire notebook ssh <notebook-name>` to bootstrap one with shared-FS access."
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
    job_id: str,
    remote_log_path: str,
    tail: int | None,
    head: int | None,
    path: bool,
    follow: bool,
    bridge_name: Optional[str] = None,
) -> None:
    try:
        config, _ = Config.from_files_and_env(
            require_credentials=False
        )

        effective_bridge_name, tunnel_config, bridge_configured = _resolve_tunnel_preflight_target(
            bridge_name
        )
        bridge_name_for_checks = effective_bridge_name or bridge_name

        try:
            tunnel_ok = is_tunnel_available(
                bridge_name=bridge_name_for_checks,
                config=tunnel_config,
                retries=0,
                retry_pause=0.0,
                progressive=False,
            )
        except TypeError:
            # Backward-compatible test doubles may still expose the old no-arg signature.
            tunnel_ok = is_tunnel_available()

        if not tunnel_ok:
            if bridge_name and tunnel_config is not None and not bridge_configured:
                _handle_error(
                    ctx,
                    "NotebookTunnelNotFound",
                    f"No cached notebook tunnel for '{bridge_name}'.",
                    EXIT_GENERAL_ERROR,
                    hint="Run 'inspire notebook connections' to see cached notebooks.",
                )
            _emit_no_tunnel_error(ctx, bridge_name=bridge_name)
            return

        if path:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json({"job_id": job_id, "log_path": remote_log_path})
                )
            else:
                click.echo(scrub_raw_ids(remote_log_path))
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

            if not ctx.json_output:
                label = f", bridge: {bridge_name}" if bridge_name else ""
                click.echo(f"Using SSH tunnel (fast path{label})")

            final_status = _follow_logs_via_ssh(
                job_id=job_id,
                config=config,
                remote_log_path=remote_log_path,
                tail_lines=tail or 50,
                bridge_name=bridge_name,
            )

            if final_status in {"SUCCEEDED", "job_succeeded"}:
                sys.exit(EXIT_SUCCESS)
            if final_status in {"FAILED", "CANCELLED", "job_failed", "job_cancelled"}:
                sys.exit(EXIT_GENERAL_ERROR)
            sys.exit(EXIT_SUCCESS)

        if not ctx.json_output:
            label = f", bridge: {bridge_name}" if bridge_name else ""
            click.echo(f"Using SSH tunnel (fast path{label})")

        try:
            content = _fetch_log_via_ssh(
                remote_log_path=remote_log_path,
                tail=tail,
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
                    "Check `inspire job status` and try again, or pass --remote-log-path "
                    "if the path differs from the default training_master_<name>.log convention."
                ),
            )
            return

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "job_id": job_id,
                        "log_path": remote_log_path,
                        "content": content,
                        "method": "ssh_tunnel",
                    }
                )
            )
            return

        if tail:
            click.echo(f"=== Last {tail} lines ===\n")
        elif head:
            click.echo(f"=== First {head} lines ===\n")
        click.echo(scrub_raw_ids(content))

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
    workspace: Optional[str],
    all_workspaces: bool,
    max_pages: int,
    instance_ids: tuple[str, ...],
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
            "--json --follow --web is not supported",
            EXIT_VALIDATION_ERROR,
            hint="Drop --json to follow web logs, or drop --follow for a one-shot JSON fetch.",
        )
        return
    if since_minutes is not None and since_minutes <= 0:
        _handle_error(
            ctx,
            "InvalidUsage",
            "--since-minutes must be positive",
            EXIT_VALIDATION_ERROR,
        )
        return
    if web_page_size <= 0:
        _handle_error(
            ctx,
            "InvalidUsage",
            "--web-page-size must be positive",
            EXIT_VALIDATION_ERROR,
        )
        return

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        job_id = _resolve_web_job_id(
            config=config,
            job=job,
            workspace=workspace,
            all_workspaces=all_workspaces,
            max_pages=max_pages,
        )

        try:
            session = get_web_session()
            job_data = browser_api_module.get_job_detail(job_id, session=session)
            if instance_ids:
                pod_names = [
                    str(instance_id or "").strip()
                    for instance_id in instance_ids
                    if str(instance_id or "").strip()
                ]
            else:
                instances, _ = browser_api_module.list_job_instances(
                    job_id,
                    num=200,
                    session=session,
                )
                pod_names = [
                    str(item.get("name") or "").strip()
                    for item in instances
                    if str(item.get("name") or "").strip()
                ]

            if not pod_names:
                _handle_error(
                    ctx,
                    "LogNotFound",
                    f"No instances found for web job {job}",
                    EXIT_LOG_NOT_FOUND,
                )
                return

            start_ms, end_ms = _web_log_time_range(job_data, since_minutes)
            fetch_size = max(web_page_size, tail or 0, head or 0)

            if follow:
                _follow_logs_via_web(
                    job_id=job_id,
                    pod_names=pod_names,
                    start_ms=start_ms,
                    tail_lines=tail or 50,
                    page_size=web_page_size,
                    session=session,
                )
                return

            logs, total = browser_api_module.list_train_job_logs(
                job_id=job_id,
                pod_names=pod_names,
                start_timestamp_ms=start_ms,
                end_timestamp_ms=end_ms,
                page_size=fetch_size,
                session=session,
            )
        finally:
            _close_web_client()

        logs = sorted(logs, key=_web_log_sort_key)
        shown = logs
        if head:
            shown = shown[:head]
        if tail:
            shown = shown[-tail:]

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "source": "web",
                        "job_id": job_id,
                        "instances": pod_names,
                        "logs": shown,
                        "total": total,
                        "returned": len(logs),
                        "shown": len(shown),
                        "time_range": {
                            "start_timestamp_ms": str(start_ms),
                            "end_timestamp_ms": str(end_ms),
                        },
                    }
                )
            )
            return

        click.echo(_format_web_logs(shown))

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
@click.argument("job")
@click.option("--tail", "-n", type=int, help="Show last N lines only")
@click.option("--head", type=int, help="Show first N lines only")
@click.option("--path", is_flag=True, help="Just print log path, don't read content")
@click.option(
    "--follow",
    "-f",
    is_flag=True,
    help="Follow new log content; default mode uses SSH tail -f, --web polls platform logs",
)
@click.option(
    "--remote-log-path",
    default=None,
    help=(
        "Explicit remote log file path. Overrides the default project log path. "
        "Useful for jobs created outside the CLI where the path was set by the platform."
    ),
)
@click.option(
    "--notebook",
    help=(
        "Notebook name whose cached SSH connection should be used. Required when more "
        "than one is cached and the default connection is ambiguous. "
        "Run `inspire notebook ssh <notebook-name>` first. "
        "No short alias — `-n` is reserved for --tail."
    ),
)
@click.option(
    "--web",
    is_flag=True,
    help=(
        "Fallback: read platform aggregated logs instead of the CLI-managed SSH log file. "
        "Use when no cached notebook SSH bridge or shared-FS log path is available."
    ),
)
@click.option("--workspace", default=None, help="Workspace name")
@click.option(
    "--all-workspaces",
    "-A",
    is_flag=True,
    help="Resolve the job name across every visible workspace, not just the current one",
)
@click.option(
    "--max-pages",
    type=int,
    default=50,
    show_default=True,
    help="Max web pages to scan per workspace when resolving a job name",
)
@click.option(
    "--instance",
    "instance_ids",
    multiple=True,
    help="Pod instance name to query. Repeat to query multiple pods.",
)
@click.option(
    "--since-minutes",
    type=int,
    default=None,
    help="Query logs from the last N minutes instead of the job lifetime window",
)
@click.option(
    "--web-page-size",
    type=int,
    default=500,
    show_default=True,
    help="Max log records fetched from the aggregated log view",
)
@pass_context
def logs(
    ctx: Context,
    job: str,
    tail: int | None,
    head: int | None,
    path: bool,
    follow: bool,
    remote_log_path: Optional[str],
    notebook: Optional[str],
    web: bool,
    workspace: Optional[str],
    all_workspaces: bool,
    max_pages: int,
    instance_ids: tuple[str, ...],
    since_minutes: int | None,
    web_page_size: int,
) -> None:
    """Read the CLI-managed remote log file for a training job over SSH.

    Requires a cached notebook connection.

    Default mode reads the log file written by `inspire job create` under
    the `me` path alias, usually
    ``<me>/.inspire/training_master_<name>_*.log``. It requires a cached
    notebook connection / SSH bridge from `inspire notebook ssh <notebook>`.

    Use ``--web`` only as a fallback to platform aggregated logs when no cached
    notebook bridge or shared-FS log path is available.

    \b
    Examples:
        inspire job logs my-training-run
        inspire job logs my-training-run --tail 100
        inspire job logs my-training-run --head 50
        inspire job logs my-training-run --follow
        inspire job logs my-training-run --path
        inspire job logs my-training-run --notebook my-cpu-box
        inspire job logs my-training-run --remote-log-path /inspire/.../custom.log
        inspire job logs --web my-training-run --tail 100
        inspire job logs --web my-training-run --follow
    """
    if notebook is not None:
        from inspire.cli.utils.id_resolver import reject_id_at_boundary

        notebook = reject_id_at_boundary(
            ctx,
            notebook,
            resource_type="notebook",
            list_command="inspire notebook connections",
        )
    bridge = notebook

    web_mode = (
        web
        or workspace
        or bool(instance_ids)
        or since_minutes is not None
    )
    if web_mode:
        if bridge:
            _handle_error(
                ctx,
                "InvalidUsage",
                "--notebook cannot be combined with --web",
                EXIT_VALIDATION_ERROR,
            )
            return
        if remote_log_path:
            _handle_error(
                ctx,
                "InvalidUsage",
                "--remote-log-path cannot be combined with --web",
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
            workspace=workspace,
            all_workspaces=all_workspaces,
            max_pages=max_pages,
            instance_ids=instance_ids,
            since_minutes=since_minutes,
            web_page_size=web_page_size,
        )
        return

    job_id = resolve_job_id(ctx, job, all_workspaces=all_workspaces)

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
        try:
            config, _ = Config.from_files_and_env(
                require_credentials=False
            )
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return
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
                    f"No log file matches {glob_pattern!r} on the shared filesystem.",
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
        job_id=job_id,
        remote_log_path=resolved_log_path,
        tail=tail,
        head=head,
        path=path,
        follow=follow,
        bridge_name=bridge,
    )


__all__ = ["logs"]
