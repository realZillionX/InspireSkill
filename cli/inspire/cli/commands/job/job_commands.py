"""Job subcommands (excluding create/logs)."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import Optional

import click

from . import job_deps
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_JOB_NOT_FOUND,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.job_cli import resolve_job_id
from inspire.config import Config, ConfigError


_STATUS_ALIAS_MAP = {
    "PENDING": {"PENDING", "job_pending", "job_creating"},
    "RUNNING": {"RUNNING", "job_running"},
    "QUEUING": {"QUEUING", "job_queuing"},
    "SUCCEEDED": {"SUCCEEDED", "job_succeeded"},
    "FAILED": {"FAILED", "job_failed"},
    "CANCELLED": {"CANCELLED", "job_cancelled", "job_stopped"},
}

_LIVE_REFRESH_STATUSES = {
    "PENDING",
    "job_pending",
    "job_creating",
    "RUNNING",
    "job_running",
    "QUEUING",
    "job_queuing",
}


def _expand_status_aliases(statuses: list[str] | tuple[str, ...] | None) -> set[str]:
    expanded: set[str] = set()
    for value in statuses or ():
        key = str(value).upper()
        expanded.update(_STATUS_ALIAS_MAP.get(key, {str(value)}))
    return expanded


def _refresh_live_jobs_from_web_api(cache, jobs: list[dict]) -> list[dict]:  # noqa: ANN001
    """Best-effort live refresh for cached active jobs using the web job list API."""
    target_ids = {
        str(job.get("job_id") or "").strip()
        for job in jobs
        if str(job.get("status") or "") in _LIVE_REFRESH_STATUSES
    }
    target_ids.discard("")
    if not target_ids:
        return jobs

    try:
        from inspire.platform.web.browser_api.jobs import list_jobs as web_list_jobs
        from inspire.platform.web.session import get_web_session

        try:
            session = get_web_session(require_workspace=True)
        except TypeError:
            session = get_web_session()
        refreshed: dict[str, str] = {}
        page_size = 100
        seen_workspaces: set[str] = set()
        workspace_ids: list[str] = []

        primary_workspace = str(getattr(session, "workspace_id", "") or "").strip()
        if primary_workspace:
            workspace_ids.append(primary_workspace)
            seen_workspaces.add(primary_workspace)

        for workspace_id in getattr(session, "all_workspace_ids", []) or []:
            wid = str(workspace_id or "").strip()
            if not wid or wid in seen_workspaces:
                continue
            workspace_ids.append(wid)
            seen_workspaces.add(wid)

        for workspace_id in workspace_ids or [""]:
            page_num = 1
            total = None
            while target_ids - refreshed.keys():
                items, total = web_list_jobs(
                    workspace_id=workspace_id or None,
                    page_num=page_num,
                    page_size=page_size,
                    session=session,
                )
                if not items:
                    break
                for item in items:
                    if item.job_id in target_ids and item.status:
                        refreshed[item.job_id] = item.status
                if total is not None and page_num * page_size >= int(total):
                    break
                page_num += 1
                if total is None and page_num > 50:
                    break
            if not (target_ids - refreshed.keys()):
                break

        for job in jobs:
            job_id = str(job.get("job_id") or "").strip()
            new_status = refreshed.get(job_id)
            if not new_status:
                continue
            if job.get("status") != new_status:
                job["status"] = new_status
                cache.update_status(job_id, new_status)
    except Exception:
        return jobs

    return jobs


def _watch_jobs(
    ctx: Context,
    config: Config,
    limit: int,
    status: Optional[str],
    active: bool,
    interval: int,
) -> None:
    """Continuously poll and display job status with incremental updates."""
    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level
    api_logger.setLevel(logging.CRITICAL)

    cache = job_deps.JobCache(config.get_expanded_cache_path())

    if not ctx.json_output:
        click.echo("🔐 Authenticating...")

    try:
        api = AuthManager.get_api(config)
    except AuthenticationError as e:
        api_logger.setLevel(original_level)
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
        return

    exclude_statuses = None
    if active:
        exclude_statuses = {"FAILED", "job_failed", "CANCELLED", "job_cancelled", "job_stopped"}

    terminal_statuses = {
        "SUCCEEDED",
        "job_succeeded",
        "FAILED",
        "job_failed",
        "CANCELLED",
        "job_cancelled",
        "job_stopped",
    }

    completed_this_session: list = []
    completed_job_ids: set = set()

    def _progress_bar(current: int, total: int, width: int = 20) -> str:
        if total == 0:
            return "░" * width
        filled = int(width * current / total)
        return "█" * filled + "░" * (width - filled)

    def _render_display(
        jobs_list: list,
        updated_count: int,
        total_count: int,
        completed_list: list,
    ) -> None:
        if not ctx.json_output:
            os.system("clear")
        if ctx.json_output:
            timestamp = datetime.now().strftime("%H:%M:%S")
            click.echo(
                json_formatter.format_json(
                    {
                        "event": "refresh",
                        "timestamp": timestamp,
                        "updated": updated_count,
                        "total": total_count,
                        "jobs": jobs_list,
                        "completed_this_session": completed_list,
                    }
                )
            )
        else:
            bar = _progress_bar(updated_count, total_count)
            if updated_count < total_count:
                click.echo(f"🔄 [{bar}] {updated_count}/{total_count} updating...\n")
            else:
                click.echo(f"✅ [{bar}] {total_count}/{total_count} done (interval: {interval}s)\n")

            click.echo(human_formatter.format_job_list(jobs_list))

            if completed_list:
                click.echo(f"\n✅ Completed This Session ({len(completed_list)})")
                click.echo("─" * 60)
                for job_item in completed_list:
                    status_emoji = (
                        "✅" if "succeeded" in job_item.get("status", "").lower() else "❌"
                    )
                    click.echo(
                        f"{job_item.get('job_id', 'N/A')[:36]:36}  "
                        f"{job_item.get('name', 'N/A')[:20]:20}  "
                        f"{status_emoji} {job_item.get('status', 'N/A')}"
                    )

    try:
        while True:
            jobs = cache.list_jobs(limit=limit, status=status, exclude_statuses=exclude_statuses)
            total = len(jobs)

            _render_display(jobs, 0, total, completed_this_session)

            for i, job_item in enumerate(jobs):
                job_id = job_item.get("job_id")
                if job_id:
                    original_status = job_item.get("status", "")
                    try:
                        result = api.get_job_detail(job_id)
                        data = result.get("data", {})
                        new_status = data.get("status")
                        if new_status:
                            job_item["status"] = new_status
                            cache.update_status(job_id, new_status)

                            if (
                                new_status in terminal_statuses
                                and original_status not in terminal_statuses
                                and job_id not in completed_job_ids
                            ):
                                completed_this_session.append(dict(job_item))
                                completed_job_ids.add(job_id)
                    except Exception:
                        pass

                _render_display(jobs, i + 1, total, completed_this_session)

                if i < total - 1:
                    job_deps.time.sleep(1.0)

            if active and exclude_statuses:
                filtered = [j for j in jobs if j.get("status") not in exclude_statuses]
                if len(filtered) != len(jobs):
                    _render_display(filtered, total, total, completed_this_session)

            job_deps.time.sleep(interval)

    except KeyboardInterrupt:
        if not ctx.json_output:
            click.echo("\nStopped watching.")
        sys.exit(EXIT_SUCCESS)
    finally:
        api_logger.setLevel(original_level)


@click.command("list")
@click.option(
    "--limit",
    "-n",
    type=int,
    default=0,
    help="Max jobs to show (0 = all, default: all)",
)
@click.option("--status", "-s", help="Filter by status (PENDING, RUNNING, SUCCEEDED, FAILED)")
@click.option(
    "--active",
    "-a",
    is_flag=True,
    help="Show only active jobs (exclude failed, cancelled, stopped)",
)
@click.option("--watch", "-w", is_flag=True, help="Continuously refresh job list")
@click.option(
    "--interval",
    type=int,
    default=10,
    help="Refresh interval in seconds for --watch (default: 10)",
)
@pass_context
def list_jobs(
    ctx: Context,
    limit: int,
    status: Optional[str],
    active: bool,
    watch: bool,
    interval: int,
) -> None:
    """List recent jobs from local cache with best-effort live status refresh.

    The local cache remains the source of truth for which jobs are shown, but
    active jobs are opportunistically refreshed against the web job list API so
    the displayed status does not lag far behind the web UI.

    \b
    Example:
        inspire job list
        inspire job list --limit 20 --status RUNNING
        inspire job list --active
        inspire job list --watch --active -n 20
        inspire job list --watch --interval 5
    """
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)

        if watch:
            _watch_jobs(
                ctx=ctx,
                config=config,
                limit=limit,
                status=status,
                active=active,
                interval=interval,
            )
            return

        cache = job_deps.JobCache(config.get_expanded_cache_path())
        jobs = cache.list_jobs(limit=0)
        jobs = _refresh_live_jobs_from_web_api(cache, jobs)

        exclude_statuses = None
        if active:
            exclude_statuses = {
                "FAILED",
                "job_failed",
                "CANCELLED",
                "job_cancelled",
                "job_stopped",
            }

        if status:
            allowed_statuses = _expand_status_aliases([status])
            jobs = [j for j in jobs if j.get("status") in allowed_statuses]

        if exclude_statuses:
            jobs = [j for j in jobs if j.get("status") not in exclude_statuses]

        jobs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        if limit is not None and limit > 0:
            jobs = jobs[:limit]

        if ctx.json_output:
            click.echo(json_formatter.format_json(jobs))
        else:
            click.echo(human_formatter.format_job_list(jobs))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


@click.command("status")
@click.argument("job_id")
@pass_context
def status(ctx: Context, job_id: str) -> None:
    """Check the status of a training job.

    \b
    Example:
        inspire job status job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf
    """
    job_id = resolve_job_id(ctx, job_id)

    try:
        config, _ = Config.from_files_and_env()
        api = AuthManager.get_api(config)

        result = api.get_job_detail(job_id)
        job_data = result.get("data", {})

        if job_data.get("status"):
            cache = job_deps.JobCache(config.get_expanded_cache_path())
            cache.update_status(job_id, job_data["status"])

        if ctx.json_output:
            click.echo(json_formatter.format_json(job_data))
        else:
            click.echo(human_formatter.format_job_status(job_data))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "invalid job id" in msg:
            _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
        else:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("stop")
@click.argument("job_id")
@pass_context
def stop(ctx: Context, job_id: str) -> None:
    """Stop a running training job.

    \b
    Example:
        inspire job stop job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf
    """
    job_id = resolve_job_id(ctx, job_id)

    try:
        config, _ = Config.from_files_and_env()
        api = AuthManager.get_api(config)

        api.stop_training_job(job_id)

        cache = job_deps.JobCache(config.get_expanded_cache_path())
        cache.update_status(job_id, "CANCELLED")

        if ctx.json_output:
            click.echo(json_formatter.format_json({"job_id": job_id, "status": "stopped"}))
        else:
            click.echo(human_formatter.format_success(f"Job stopped: {job_id}"))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "invalid job id" in msg:
            _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
        else:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("wait")
@click.argument("job_id")
@click.option("--timeout", type=int, default=14400, help="Timeout in seconds (default: 4 hours)")
@click.option("--interval", type=int, default=30, help="Poll interval in seconds (default: 30)")
@pass_context
def wait(ctx: Context, job_id: str, timeout: int, interval: int) -> None:
    """Wait for a job to complete.

    Polls the job status until it reaches a terminal state
    (SUCCEEDED, FAILED, or CANCELLED).

    \b
    Example:
        inspire job wait job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf --timeout 7200
    """
    job_id = resolve_job_id(ctx, job_id)

    try:
        config, _ = Config.from_files_and_env()
        api = AuthManager.get_api(config)
        cache = job_deps.JobCache(config.get_expanded_cache_path())

        terminal_statuses = {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "job_succeeded",
            "job_failed",
            "job_cancelled",
        }
        start_time = job_deps.time.time()
        last_status = None

        if not ctx.json_output:
            click.echo(f"Waiting for job {job_id} (timeout: {timeout}s, interval: {interval}s)")

        while True:
            elapsed = job_deps.time.time() - start_time

            if elapsed > timeout:
                _handle_error(ctx, "Timeout", f"Timeout after {timeout}s", EXIT_TIMEOUT)
                return

            try:
                result = api.get_job_detail(job_id)
                job_data = result.get("data", {})
                current_status = job_data.get("status", "UNKNOWN")

                cache.update_status(job_id, current_status)

                if current_status != last_status:
                    if ctx.json_output:
                        click.echo(
                            json_formatter.format_json(
                                {
                                    "event": "status_change",
                                    "status": current_status,
                                    "elapsed_seconds": int(elapsed),
                                }
                            )
                        )
                    else:
                        click.echo(f"\nStatus: {current_status}")
                    last_status = current_status
                else:
                    if not ctx.json_output:
                        mins = int(elapsed // 60)
                        secs = int(elapsed % 60)
                        click.echo(
                            f"\r[{mins:02d}:{secs:02d}] Waiting... Status: {current_status}",
                            nl=False,
                        )

                if current_status in terminal_statuses:
                    if ctx.json_output:
                        click.echo(json_formatter.format_json(job_data))
                    else:
                        click.echo("")
                        click.echo(human_formatter.format_job_status(job_data))

                    if current_status in {"SUCCEEDED", "job_succeeded"}:
                        sys.exit(EXIT_SUCCESS)
                    sys.exit(EXIT_GENERAL_ERROR)

            except Exception as e:
                if not ctx.json_output:
                    click.echo(f"\nWarning: Failed to get status: {e}")

            job_deps.time.sleep(interval)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except KeyboardInterrupt:
        if not ctx.json_output:
            click.echo("\nInterrupted")
        sys.exit(EXIT_GENERAL_ERROR)


@click.command("update")
@click.option(
    "--status",
    "-s",
    multiple=True,
    help="Status filter (default: PENDING,RUNNING + API aliases). Repeatable.",
)
@click.option(
    "--limit",
    "-n",
    type=int,
    default=0,
    help="Max jobs to refresh from cache (0 = all, default: all)",
)
@click.option(
    "--delay",
    "-d",
    type=float,
    default=0.6,
    help="Delay between API requests in seconds to avoid rate limits (default: 0.6)",
)
@pass_context
def update_jobs(ctx: Context, status: tuple, limit: int, delay: float) -> None:
    """Update cached jobs by polling the API.

    Refreshes statuses for cached jobs matching the status filter
    (defaults to PENDING/RUNNING/QUEUING and API snake_case aliases) and
    updates the local cache. Skips jobs that fail to refresh and
    reports them.
    """
    default_statuses = ("PENDING", "RUNNING", "QUEUING") if not status else tuple(status)
    alias_map = {
        "PENDING": {"PENDING", "job_pending", "job_creating"},
        "RUNNING": {"RUNNING", "job_running"},
        "QUEUING": {"QUEUING", "job_queuing"},
        "SUCCEEDED": {"SUCCEEDED", "job_succeeded"},
        "FAILED": {"FAILED", "job_failed"},
        "CANCELLED": {"CANCELLED", "job_cancelled"},
    }
    statuses_set = set()
    for s in default_statuses:
        key = str(s).upper()
        statuses_set.update(alias_map.get(key, {s}))

    try:
        config, _ = Config.from_files_and_env()
        api = AuthManager.get_api(config)
        cache = job_deps.JobCache(config.get_expanded_cache_path())

        jobs = cache.list_jobs(limit=limit)
        jobs = [j for j in jobs if j.get("status") in statuses_set]

        updated = []
        errors = []

        for job in jobs:
            job_id = job.get("job_id")
            if not job_id:
                continue
            old_status = job.get("status", "UNKNOWN")
            try:
                result = api.get_job_detail(job_id)
                data = result.get("data", {}) if isinstance(result, dict) else {}
                new_status = data.get("status") or data.get("job_status") or old_status
                if new_status:
                    cache.update_status(job_id, new_status)
                updated.append(
                    {
                        "job_id": job_id,
                        "old_status": old_status,
                        "new_status": new_status,
                    }
                )
            except Exception as e:  # noqa: BLE001
                errors.append({"job_id": job_id, "error": str(e)})
            if delay > 0:
                job_deps.time.sleep(delay)

        if ctx.json_output:
            payload = {
                "updated": updated,
                "errors": errors,
            }
            click.echo(json_formatter.format_json(payload))
            return

        if updated:
            refreshed_jobs = [cache.get_job(u["job_id"]) for u in updated]
            refreshed_jobs = [j for j in refreshed_jobs if j]
            click.echo(human_formatter.format_job_list(refreshed_jobs))
        else:
            click.echo("\nNo matching jobs to update.\n")

        if errors:
            click.echo("\nErrors during update:")
            for err in errors:
                click.echo(f"- {err['job_id']}: {err['error']}")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("command")
@click.argument("job_id")
@pass_context
def show_command(ctx: Context, job_id: str) -> None:
    """Show the training command used for a job."""
    job_id = resolve_job_id(ctx, job_id)

    cached_command = None
    cache = job_deps.JobCache(os.getenv("INSPIRE_JOB_CACHE"))
    cached_job = cache.get_job(job_id)
    if cached_job:
        cached_command = cached_job.get("command")

    command_value = None
    source = None

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        api = AuthManager.get_api(config)

        result = api.get_job_detail(job_id)
        job_data = result.get("data", {})
        command_value = job_data.get("command")
        if command_value:
            source = "api"
    except ConfigError as e:
        if not cached_command:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return
    except AuthenticationError as e:
        if not cached_command:
            _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
            return
    except Exception as e:
        if not cached_command:
            msg = str(e).lower()
            if "not found" in msg or "invalid job id" in msg:
                _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
            else:
                _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
            return

    if not command_value and cached_command:
        command_value = cached_command
        source = "cache"

    if not command_value:
        _handle_error(
            ctx,
            "CommandNotFound",
            f"No command found for job {job_id}",
            EXIT_API_ERROR,
        )
        return

    if ctx.json_output:
        payload = {"job_id": job_id, "command": command_value}
        if source:
            payload["source"] = source
        click.echo(json_formatter.format_json(payload))
    else:
        click.echo(command_value)


__all__ = [
    "list_jobs",
    "show_command",
    "status",
    "stop",
    "update_jobs",
    "wait",
]
