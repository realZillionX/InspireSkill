"""Job logs command.

Implements `inspire job logs` including:
- Single-job mode (with JOB_ID)
- Bulk mode (without JOB_ID)
- SSH tunnel fast-path
"""

from __future__ import annotations

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

import click

from inspire.bridge.forge import (
    ForgeAuthError,
    ForgeError,
    _get_active_repo,
    _get_active_server,
    fetch_remote_log_incremental,
)
from inspire.bridge.tunnel import (
    TunnelNotAvailableError,
    _test_ssh_connection,
    is_tunnel_available,
    load_tunnel_config,
    run_ssh_command,
)
from . import job_deps
from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_JOB_NOT_FOUND,
    EXIT_LOG_NOT_FOUND,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.auth import AuthManager
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.job_cli import resolve_job_id
from inspire.config import Config, ConfigError


class _JobCacheProtocol(Protocol):
    def get_log_offset(self, job_id: str) -> int: ...

    def reset_log_offset(self, job_id: str) -> None: ...

    def set_log_offset(self, job_id: str, offset: int) -> None: ...


@dataclass(frozen=True)
class JobLogCachePaths:
    cache_path: Path
    legacy_cache_path: Path


def _build_log_cache_paths(config: Config, job_id: str) -> JobLogCachePaths:
    cache_dir = Path(os.path.expanduser(config.log_cache_dir))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return JobLogCachePaths(
        cache_path=cache_dir / f"{job_id}.log",
        legacy_cache_path=cache_dir / f"job-{job_id}.log",
    )


def _migrate_legacy_log_filename(paths: JobLogCachePaths) -> Path:
    cache_path = paths.cache_path
    legacy_cache_path = paths.legacy_cache_path

    if not cache_path.exists() and legacy_cache_path.exists():
        try:
            legacy_cache_path.replace(cache_path)
            return cache_path
        except OSError:
            return legacy_cache_path

    return cache_path


def _get_current_log_offset(
    cache: _JobCacheProtocol,
    *,
    job_id: str,
    cache_path: Path,
    refresh: bool,
) -> int:
    current_offset = 0 if refresh else cache.get_log_offset(job_id)

    if current_offset > 0 and not cache_path.exists():
        cache.reset_log_offset(job_id)
        return 0

    return current_offset


def _update_log_offset_to_filesize(
    cache: _JobCacheProtocol, *, job_id: str, cache_path: Path
) -> None:
    if cache_path.exists():
        cache.set_log_offset(job_id, cache_path.stat().st_size)


def _format_remote_log_error_message(
    err: Exception, *, remote_log_path: str, config: Config
) -> str:
    try:
        actions_url = f"{_get_active_server(config)}/{_get_active_repo(config)}/actions"
    except Exception:
        actions_url = "configure INSP_GITHUB_REPO first"
    return (
        f"{str(err)}\n\n"
        f"Hints:\n"
        f"- Check that the training job created a log file at: {remote_log_path}\n"
        f"- Verify the Bridge workflow exists and can access the shared filesystem\n"
        f"- View GitHub Actions at: {actions_url}"
    )


def _fetch_log_incremental(
    *,
    config: Config,
    job_id: str,
    remote_log_path: str,
    cache_path: Path,
    start_offset: int,
) -> int:
    _, bytes_added = fetch_remote_log_incremental(
        config=config,
        job_id=job_id,
        remote_log_path=remote_log_path,
        cache_path=cache_path,
        start_offset=start_offset,
    )
    return bytes_added


def _fetch_log_full_via_bridge(
    *,
    config: Config,
    job_id: str,
    remote_log_path: str,
    cache_path: Path,
    refresh: bool,
) -> None:
    job_deps.fetch_remote_log_via_bridge(
        config=config,
        job_id=job_id,
        remote_log_path=remote_log_path,
        cache_path=cache_path,
        refresh=refresh,
    )


def _echo_log_path(ctx: Context, *, job_id: str, remote_log_path: str) -> None:
    if ctx.json_output:
        click.echo(json_formatter.format_json({"job_id": job_id, "log_path": remote_log_path}))
    else:
        click.echo(remote_log_path)


def _echo_ssh_content(
    ctx: Context,
    *,
    job_id: str,
    remote_log_path: str,
    content: str,
    tail: int | None,
    head: int | None,
) -> None:
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
    click.echo(content)


def _echo_file_tail(ctx: Context, *, cache_path: Path, tail: int) -> None:
    with cache_path.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    tail_lines = lines[-tail:] if tail > 0 else lines

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "log_path": str(cache_path),
                    "lines": tail_lines,
                    "count": len(tail_lines),
                }
            )
        )
    else:
        click.echo(f"=== Last {len(tail_lines)} lines ===\n")
        for line in tail_lines:
            click.echo(line)


def _echo_file_head(ctx: Context, *, cache_path: Path, head: int) -> None:
    with cache_path.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    head_lines = lines[:head] if head > 0 else lines

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "log_path": str(cache_path),
                    "lines": head_lines,
                    "count": len(head_lines),
                }
            )
        )
    else:
        click.echo(f"=== First {len(head_lines)} lines ===\n")
        for line in head_lines:
            click.echo(line)


def _echo_file_content(ctx: Context, *, cache_path: Path) -> None:
    content = cache_path.read_text(encoding="utf-8", errors="replace")

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "log_path": str(cache_path),
                    "content": content,
                    "size_bytes": len(content),
                }
            )
        )
    else:
        click.echo(content)


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

    click.echo(f"Log file: {remote_log_path}")

    check_cmd = f"test -f '{remote_log_path}' && echo 'exists' || echo 'waiting'"
    start_time = time.time()
    file_exists = False

    while time.time() - start_time < wait_timeout:
        try:
            result = run_ssh_command(check_cmd, timeout=10, bridge_name=bridge_name)
            if "exists" in result.stdout:
                file_exists = True
                break
        except Exception:
            pass

        elapsed = int(time.time() - start_time)
        click.echo(f"\rWaiting for job to start... ({elapsed}s)", nl=False)
        time.sleep(5)

    if not file_exists:
        click.echo(f"\n\nTimeout: Log file not created after {wait_timeout}s")
        click.echo("Job may still be queuing. Check status with: inspire job status <job-name>")
        return None

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

        last_status_check = time.time()

        while True:
            if process.poll() is not None:
                for line in process.stdout:
                    click.echo(line, nl=False)
                break

            ready, _, _ = select.select([process.stdout], [], [], 1.0)

            if ready:
                line = process.stdout.readline()
                if line:
                    click.echo(line, nl=False)
                elif process.poll() is not None:
                    break

            current_time = time.time()
            if current_time - last_status_check >= status_check_interval:
                last_status_check = current_time
                try:
                    result = api.get_job_detail(job_id)
                    job_data = result.get("data", {})
                    current_status = job_data.get("status", "UNKNOWN")

                    if current_status in terminal_statuses:
                        final_status = current_status
                        time.sleep(3)
                        process.stdout.close()
                        break
                except Exception:
                    pass

        if final_status:
            click.echo(f"\n\nJob completed with status: {final_status}")

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


def _emit_tunnel_fallback_hint(ctx: Context, *, bridge_name: Optional[str]) -> None:
    if ctx.json_output:
        return

    target_label = f"bridge '{bridge_name}'" if bridge_name else "default bridge"
    click.echo(f"Tunnel {target_label} not available, using GitHub workflow...", err=True)

    connected = _find_connected_tunnel_bridges(exclude=bridge_name)
    if connected:
        preview = connected[:5]
        preview_text = ", ".join(preview)
        remaining = len(connected) - len(preview)
        if remaining > 0:
            preview_text = f"{preview_text}, +{remaining} more"
        click.echo(
            f"Cached notebook tunnel(s): {preview_text}. "
            "Use --notebook <name> to target one explicitly.",
            err=True,
        )


def _resolve_tunnel_preflight_target(
    bridge_name: Optional[str],
) -> tuple[Optional[str], object | None, bool]:
    """Resolve the bridge/config tuple used by SSH availability preflight.

    Returns:
        (effective_bridge_name, tunnel_config_or_none, has_configured_bridge)
    """
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


def _try_get_ssh_exit_code(
    ctx: Context,
    *,
    config: Config,
    job_id: str,
    remote_log_path: str,
    tail: int | None,
    head: int | None,
    path: bool,
    follow: bool,
    refresh: bool,
    cache_exists: bool,
    current_offset: int,
    bridge_name: Optional[str] = None,
) -> int | None:
    effective_bridge_name, tunnel_config, bridge_configured = _resolve_tunnel_preflight_target(
        bridge_name
    )
    bridge_name_for_checks = effective_bridge_name or bridge_name

    requires_remote_fetch = (not path) and (
        follow or refresh or (not cache_exists) or current_offset > 0
    )

    def _check_tunnel_available() -> bool:
        try:
            return is_tunnel_available(
                bridge_name=bridge_name_for_checks,
                config=tunnel_config,
                retries=0,
                retry_pause=0.0,
                progressive=False,
            )
        except TypeError:
            # Backward-compatible test doubles may still expose the old no-arg signature.
            return is_tunnel_available()

    try:
        if not _check_tunnel_available():
            if bridge_name and tunnel_config is not None and not bridge_configured:
                _handle_error(
                    ctx,
                    "NotebookTunnelNotFound",
                    f"No cached notebook tunnel for '{bridge_name}'.",
                    EXIT_GENERAL_ERROR,
                    hint="Run 'inspire notebook connections' to see cached notebooks.",
                )
            if bridge_configured and requires_remote_fetch:
                bridge_label = (
                    f"notebook '{bridge_name_for_checks}'"
                    if bridge_name_for_checks
                    else "default notebook"
                )
                _handle_error(
                    ctx,
                    "TunnelError",
                    f"SSH tunnel not available for {bridge_label}.",
                    EXIT_GENERAL_ERROR,
                    hint=(
                        "Run 'inspire notebook test' to troubleshoot. "
                        "If needed, re-bootstrap via "
                        "'inspire notebook ssh <notebook-name>'."
                    ),
                )
            _emit_tunnel_fallback_hint(ctx, bridge_name=bridge_name)
            return None

        if follow and ctx.json_output:
            # JSON follow mode must stay machine-readable.
            # Skip SSH tail -f output and use workflow follow path instead.
            return None
        if follow:
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
                return EXIT_SUCCESS
            if final_status in {"FAILED", "CANCELLED", "job_failed", "job_cancelled"}:
                return EXIT_GENERAL_ERROR
            return EXIT_SUCCESS

        if not ctx.json_output:
            label = f", bridge: {bridge_name}" if bridge_name else ""
            click.echo(f"Using SSH tunnel (fast path{label})")

        content = _fetch_log_via_ssh(
            remote_log_path=remote_log_path,
            tail=tail,
            head=head,
            bridge_name=bridge_name,
        )

        if path:
            _echo_log_path(ctx, job_id=job_id, remote_log_path=remote_log_path)
        else:
            _echo_ssh_content(
                ctx,
                job_id=job_id,
                remote_log_path=remote_log_path,
                content=content,
                tail=tail,
                head=head,
            )

        return EXIT_SUCCESS

    except TunnelNotAvailableError:
        _emit_tunnel_fallback_hint(ctx, bridge_name=bridge_name)
    except IOError as e:
        if not ctx.json_output:
            click.echo(f"SSH log fetch failed: {e}", err=True)
            click.echo("Falling back to GitHub workflow...", err=True)

    return None


def _follow_logs(
    ctx: Context,
    config: Config,
    cache,
    job_id: str,
    remote_log_path: str,
    cache_path: Path,
    refresh: bool,
    interval: int,
) -> int:
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

    try:
        current_offset = 0 if refresh else cache.get_log_offset(job_id)

        if refresh or not cache_path.exists():
            if not ctx.json_output:
                click.echo(f"Fetching log for job {job_id}...")

            try:
                _fetch_log_full_via_bridge(
                    config=config,
                    job_id=job_id,
                    remote_log_path=remote_log_path,
                    cache_path=cache_path,
                    refresh=refresh,
                )
                current_offset = cache_path.stat().st_size
                cache.set_log_offset(job_id, current_offset)
            except (ForgeAuthError, ForgeError, TimeoutError) as e:
                _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)

        if cache_path.exists():
            content = cache_path.read_text(encoding="utf-8", errors="replace")
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json(
                        {
                            "event": "initial_content",
                            "job_id": job_id,
                            "size_bytes": len(content),
                            "content": content,
                        }
                    )
                )
            else:
                click.echo(content, nl=False)

            current_offset = cache_path.stat().st_size
            cache.set_log_offset(job_id, current_offset)

        last_displayed = current_offset

        if not ctx.json_output:
            click.echo(f"\n--- Following log (interval: {interval}s, Ctrl+C to stop) ---")

        while True:
            job_deps.time.sleep(interval)

            try:
                _fetch_log_full_via_bridge(
                    config=config,
                    job_id=job_id,
                    remote_log_path=remote_log_path,
                    cache_path=cache_path,
                    refresh=True,
                )

                size_after = cache_path.stat().st_size if cache_path.exists() else 0
                bytes_added = size_after - last_displayed

                if bytes_added > 0:
                    current_offset = size_after
                    cache.set_log_offset(job_id, current_offset)

                    with cache_path.open("rb") as f:
                        f.seek(last_displayed)
                        new_content = f.read().decode("utf-8", errors="replace")

                    if ctx.json_output:
                        click.echo(
                            json_formatter.format_json(
                                {
                                    "event": "new_content",
                                    "job_id": job_id,
                                    "bytes_added": bytes_added,
                                    "offset": current_offset,
                                    "content": new_content,
                                }
                            )
                        )
                    else:
                        click.echo(new_content, nl=False)

                    last_displayed = current_offset

            except (ForgeError, TimeoutError) as e:
                if not ctx.json_output:
                    click.echo(f"\nWarning: Fetch failed: {e}", err=True)

            try:
                result = api.get_job_detail(job_id)
                job_data = result.get("data", {})
                current_status = job_data.get("status", "UNKNOWN")
                cache.update_status(job_id, current_status)

                if current_status in terminal_statuses:
                    final_status = current_status
                    break
            except Exception as e:
                if not ctx.json_output:
                    click.echo(f"\nWarning: Status check failed: {e}", err=True)

        if final_status:
            if not ctx.json_output:
                click.echo(f"\n--- Job completed with status: {final_status} ---")
                click.echo("Fetching final log content...")

            _fetch_log_full_via_bridge(
                config=config,
                job_id=job_id,
                remote_log_path=remote_log_path,
                cache_path=cache_path,
                refresh=True,
            )

            size_after = cache_path.stat().st_size if cache_path.exists() else 0
            bytes_added = size_after - last_displayed

            if bytes_added > 0:
                with cache_path.open("rb") as f:
                    f.seek(last_displayed)
                    new_content = f.read().decode("utf-8", errors="replace")

                if ctx.json_output:
                    click.echo(
                        json_formatter.format_json(
                            {
                                "event": "final_content",
                                "job_id": job_id,
                                "status": final_status,
                                "bytes_added": bytes_added,
                                "content": new_content,
                            }
                        )
                    )
                else:
                    click.echo(new_content, nl=False)

        if final_status in {"SUCCEEDED", "job_succeeded"}:
            return EXIT_SUCCESS
        if final_status in {"FAILED", "CANCELLED", "job_failed", "job_cancelled"}:
            return EXIT_GENERAL_ERROR
        return EXIT_SUCCESS

    except KeyboardInterrupt:
        if not ctx.json_output:
            click.echo("\nStopped following.")
        return EXIT_SUCCESS
    except ForgeAuthError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return EXIT_CONFIG_ERROR


def _bulk_update_logs(
    ctx: Context,
    status: tuple,
    limit: int,
    refresh: bool,
) -> None:
    try:
        config = Config.from_env(require_target_dir=False)
        cache = job_deps.JobCache(config.get_expanded_cache_path())

        alias_map = {
            "PENDING": {"PENDING", "job_pending"},
            "RUNNING": {"RUNNING", "job_running"},
            "SUCCEEDED": {"SUCCEEDED", "job_succeeded"},
            "FAILED": {"FAILED", "job_failed"},
            "CANCELLED": {"CANCELLED", "job_cancelled"},
        }

        status_filter = set()
        if status:
            for s in status:
                key = str(s).upper()
                status_filter.update(alias_map.get(key, {s}))

        jobs = cache.list_jobs(limit=limit)
        if status_filter:
            jobs = [j for j in jobs if j.get("status") in status_filter]

        total_candidates = len(jobs)

        cache_dir = Path(os.path.expanduser(config.log_cache_dir))
        cache_dir.mkdir(parents=True, exist_ok=True)

        updated = []
        errors = []
        skipped_no_log = []

        for job in jobs:
            job_id_item = job.get("job_id")
            remote_log_path_str = job.get("log_path")

            if not job_id_item:
                continue

            if not remote_log_path_str:
                skipped_no_log.append(job_id_item)
                continue

            cache_path = cache_dir / f"{job_id_item}.log"

            try:
                _fetch_log_full_via_bridge(
                    config=config,
                    job_id=job_id_item,
                    remote_log_path=str(remote_log_path_str),
                    cache_path=cache_path,
                    refresh=refresh,
                )
                updated.append({"job_id": job_id_item, "log_path": str(cache_path)})
            except ForgeAuthError as e:
                _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            except TimeoutError as e:
                errors.append({"job_id": job_id_item, "error": str(e)})
            except ForgeError as e:
                error_msg = _format_remote_log_error_message(
                    e,
                    remote_log_path=str(remote_log_path_str),
                    config=config,
                )
                errors.append({"job_id": job_id_item, "error": error_msg})
            except Exception as e:  # noqa: BLE001
                errors.append({"job_id": job_id_item, "error": str(e)})

        success_flag = not errors

        payload = {
            "updated": updated,
            "errors": errors,
            "skipped_no_log_path": skipped_no_log,
            "processed": total_candidates,
            "fetched": len(updated),
            "refresh": refresh,
            "status_filter": sorted(status_filter),
            "limit": limit,
        }

        if ctx.json_output:
            click.echo(json_formatter.format_json(payload, success=success_flag))
            if not success_flag:
                sys.exit(EXIT_GENERAL_ERROR)
            return

        if not jobs:
            click.echo("No cached jobs matched the filter.")
            return

        status_label = f" with status in {sorted(status_filter)}" if status_filter else ""
        click.echo(
            f"Updating logs for {total_candidates} cached job(s){status_label} (refresh={refresh})"
        )

        if updated:
            click.echo("\nFetched:")
            for entry in updated:
                click.echo(f"- {entry['job_id']}: {entry['log_path']}")

        if skipped_no_log:
            click.echo("\nSkipped (no log_path in cache): " + ", ".join(skipped_no_log))

        if errors:
            click.echo("\nErrors:")
            for err in errors:
                click.echo(f"- {err['job_id']}: {err['error']}")
            sys.exit(EXIT_GENERAL_ERROR)

        click.echo("\nDone.")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


def _run_job_logs_single_job(
    ctx: Context,
    *,
    job_id: str,
    tail: int | None,
    head: int | None,
    path: bool,
    refresh: bool,
    follow: bool,
    interval: int,
    bridge: Optional[str] = None,
) -> None:
    try:
        config = Config.from_env(require_target_dir=False)
        cache = job_deps.JobCache(config.get_expanded_cache_path())

        cached = cache.get_job(job_id)
        if not cached:
            _handle_error(ctx, "JobNotFound", f"Job not found: {job_id}", EXIT_JOB_NOT_FOUND)
            return

        remote_log_path_str = cached.get("log_path")
        if not remote_log_path_str:
            _handle_error(
                ctx,
                "LogNotFound",
                f"No log file found for job {job_id}",
                EXIT_LOG_NOT_FOUND,
            )
            return

        cache_paths = _build_log_cache_paths(config, job_id)
        cache_path = _migrate_legacy_log_filename(cache_paths)
        cache_exists = cache_path.exists()
        current_offset = _get_current_log_offset(
            cache,
            job_id=job_id,
            cache_path=cache_path,
            refresh=refresh,
        )

        ssh_exit_code = _try_get_ssh_exit_code(
            ctx,
            config=config,
            job_id=job_id,
            remote_log_path=str(remote_log_path_str),
            tail=tail,
            head=head,
            path=path,
            follow=follow,
            refresh=refresh,
            cache_exists=cache_exists,
            current_offset=current_offset,
            bridge_name=bridge,
        )
        if ssh_exit_code is not None:
            sys.exit(ssh_exit_code)

        if path:
            _echo_log_path(ctx, job_id=job_id, remote_log_path=str(remote_log_path_str))
            sys.exit(EXIT_SUCCESS)

        if follow:
            follow_exit_code = _follow_logs(
                ctx=ctx,
                config=config,
                cache=cache,
                job_id=job_id,
                remote_log_path=str(remote_log_path_str),
                cache_path=cache_path,
                refresh=refresh,
                interval=interval,
            )
            sys.exit(follow_exit_code)

        if current_offset > 0 and cache_exists:
            if not ctx.json_output:
                click.echo(f"Fetching new log content from offset {current_offset}...")

            try:
                bytes_added = _fetch_log_incremental(
                    config=config,
                    job_id=job_id,
                    remote_log_path=str(remote_log_path_str),
                    cache_path=cache_path,
                    start_offset=current_offset,
                )
                cache.set_log_offset(job_id, current_offset + bytes_added)
                if not ctx.json_output and bytes_added == 0:
                    click.echo("No new content. If log was rotated, use --refresh.", err=True)
            except ForgeAuthError as e:
                _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            except TimeoutError as e:
                _handle_error(ctx, "Timeout", str(e), EXIT_TIMEOUT)
            except ForgeError as e:
                error_msg = _format_remote_log_error_message(
                    e,
                    remote_log_path=str(remote_log_path_str),
                    config=config,
                )
                _handle_error(ctx, "RemoteLogError", error_msg, EXIT_GENERAL_ERROR)

        elif refresh or not cache_path.exists():
            if not ctx.json_output:
                click.echo(
                    "Fetching remote log via GitHub workflow "
                    "(deprecated -- will be removed in a future release; "
                    "first fetch may take ~10-30s)..."
                )

            try:
                _fetch_log_full_via_bridge(
                    config=config,
                    job_id=job_id,
                    remote_log_path=str(remote_log_path_str),
                    cache_path=cache_path,
                    refresh=refresh,
                )
                _update_log_offset_to_filesize(cache, job_id=job_id, cache_path=cache_path)
            except ForgeAuthError as e:
                _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            except TimeoutError as e:
                _handle_error(ctx, "Timeout", str(e), EXIT_TIMEOUT)
            except ForgeError as e:
                error_msg = _format_remote_log_error_message(
                    e,
                    remote_log_path=str(remote_log_path_str),
                    config=config,
                )
                _handle_error(ctx, "RemoteLogError", error_msg, EXIT_GENERAL_ERROR)

        if not cache_path.exists():
            _handle_error(
                ctx,
                "LogNotFound",
                f"Failed to retrieve log for job {job_id}; the Bridge workflow may have failed.",
                EXIT_LOG_NOT_FOUND,
            )
            return

        if tail:
            try:
                _echo_file_tail(ctx, cache_path=cache_path, tail=tail)
            except OSError as e:
                _handle_error(ctx, "LogNotFound", str(e), EXIT_LOG_NOT_FOUND)
            return

        if head:
            try:
                _echo_file_head(ctx, cache_path=cache_path, head=head)
            except OSError as e:
                _handle_error(ctx, "LogNotFound", str(e), EXIT_LOG_NOT_FOUND)
            return

        try:
            _echo_file_content(ctx, cache_path=cache_path)
        except OSError as e:
            _handle_error(ctx, "LogNotFound", str(e), EXIT_LOG_NOT_FOUND)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


@click.command("logs")
@click.argument("job", required=False)
@click.option("--tail", "-n", type=int, help="Show last N lines only")
@click.option("--head", type=int, help="Show first N lines only")
@click.option("--path", is_flag=True, help="Just print log path, don't read content")
@click.option(
    "--refresh",
    is_flag=True,
    help="Re-fetch log from the beginning (ignore cached offset)",
)
@click.option("--follow", "-f", is_flag=True, help="Continuously poll for new log content")
@click.option(
    "--interval",
    type=int,
    default=30,
    help="Poll interval for --follow in seconds (default: 30)",
)
@click.option(
    "--status",
    "-s",
    multiple=True,
    help="Status filter for bulk mode (e.g., RUNNING). Repeatable.",
)
@click.option(
    "--limit",
    "-m",
    type=int,
    default=0,
    help="Max cached jobs to process in bulk mode (0 = all).",
)
@click.option(
    "--notebook",
    help=(
        "Notebook name whose cached SSH tunnel should be used as the "
        "fast path for log fetching (falls back to workflow if absent). "
        "No short alias — `-n` is reserved for --tail."
    ),
)
@pass_context
def logs(
    ctx: Context,
    job: Optional[str],
    tail: int | None,
    head: int | None,
    path: bool,
    refresh: bool,
    follow: bool,
    interval: int,
    status: tuple,
    limit: int,
    notebook: Optional[str],
) -> None:
    """View logs for a training job.

    Prefers the SSH tunnel fast path when a notebook connection is cached
    (1:1 with the notebook name; bootstrap with `inspire notebook ssh
    <notebook-name>` first). Otherwise fetches logs via GitHub workflow
    and caches them locally. Incremental fetching is enabled by default
    — only new bytes are fetched when a local cache exists. Use
    --refresh to re-fetch from the beginning.

    \b
    Single job mode (with JOB name):
        Fetches and displays the log for a specific job.

    Bulk mode (without JOB):
        Fetches and caches logs for multiple jobs from local cache.
        Use --status to filter by job status.

    \b
    Examples:
        inspire job logs my-training-run
        inspire job logs my-training-run --tail 100
        inspire job logs my-training-run --head 50
        inspire job logs my-training-run --follow
        inspire job logs my-training-run --follow --interval 10
        inspire job logs my-training-run --path
        inspire job logs my-training-run --refresh
        inspire job logs my-training-run --notebook my-cpu-box
        inspire job logs --status RUNNING --status SUCCEEDED
        inspire job logs --refresh --status RUNNING
    """
    bridge = notebook
    if not job:
        if tail or head or path or follow or bridge:
            _handle_error(
                ctx,
                "InvalidUsage",
                "--tail, --head, --path, --follow and --notebook require a JOB name",
                EXIT_VALIDATION_ERROR,
            )
            return
        _bulk_update_logs(ctx, status=status, limit=limit, refresh=refresh)
        return

    job_id = resolve_job_id(ctx, job)

    _run_job_logs_single_job(
        ctx,
        job_id=job_id,
        tail=tail,
        head=head,
        path=path,
        refresh=refresh,
        follow=follow,
        interval=interval,
        bridge=bridge,
    )


__all__ = ["logs"]
