"""Run command - Quick job submission via quota triple.

Usage:
    inspire run "python train.py" -q 8,160,1800
    inspire run "bash train.sh" --quota 4,80,800 --group H100
    inspire run "python train.py" -q 1,20,200 --watch
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

import click

from inspire.cli.context import (
    Context,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_SUCCESS,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils import job_submit
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.quota_resolver import (
    QuotaMatchError,
    QuotaParseError,
    SCHEDULE_TYPE_TRAIN,
    parse_quota,
    resolve_quota,
)
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web.session import get_web_session


def _get_current_branch() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _get_inspire_executable() -> str | None:
    return shutil.which("inspire")


def _exec_inspire_subcommand(args: list[str]) -> None:
    exe = _get_inspire_executable()
    if not exe:
        raise RuntimeError("Cannot find 'inspire' executable in PATH")
    os.execv(exe, [exe, *args])


def _run_flow(
    ctx: Context,
    *,
    command: str,
    quota: str,
    name: str | None,
    watch: bool,
    priority: int | None,
    workspace: str | None,
    group: str | None,
    max_time: float,
    image: str | None,
    nodes: int,
    project: str | None,
) -> None:
    try:
        config, _ = Config.from_files_and_env(require_target_dir=True)
        api = AuthManager.get_api(config)

        if priority is None:
            priority = config.job_priority
        if image is None:
            image = config.job_image

        try:
            quota_spec = parse_quota(quota)
        except QuotaParseError as e:
            _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
            return

        selected_workspace_id = select_workspace_id(
            config,
            explicit_workspace_name=workspace,
        )
        if not selected_workspace_id:
            _handle_error(
                ctx,
                "ConfigError",
                "No workspace_id configured. Set [context].workspace in config.toml, "
                "or pass --workspace <name>.",
                EXIT_CONFIG_ERROR,
            )
            return

        session = get_web_session()
        try:
            resolved_quota = resolve_quota(
                spec=quota_spec,
                workspace_id=selected_workspace_id,
                session=session,
                schedule_config_type=SCHEDULE_TYPE_TRAIN,
                group_override=group,
            )
        except QuotaMatchError as err:
            _handle_error(ctx, "ValidationError", str(err), EXIT_VALIDATION_ERROR)
            return

        if not name:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            branch = _get_current_branch()
            branch_suffix = f"-{branch}" if branch else ""
            name = f"run-{timestamp}{branch_suffix}"

        if ctx.debug and not ctx.json_output:
            click.echo(f"Creating job '{name}'...")
            click.echo(
                f"Using compute group: {resolved_quota.compute_group_name} "
                f"({resolved_quota.gpu_count}x{resolved_quota.gpu_type or 'CPU'})"
            )

        time.sleep(0.5)

        try:
            selected_project, fallback_msg = job_submit.select_project_for_workspace(
                config,
                workspace_id=selected_workspace_id,
                requested=project,
            )
        except ValueError as e:
            error_type = "QuotaExceeded" if "over quota" in str(e) else "ValidationError"
            _handle_error(ctx, error_type, str(e), EXIT_CONFIG_ERROR)
            return
        project_id = selected_project.project_id

        if not ctx.json_output and fallback_msg:
            click.echo(fallback_msg)
        if ctx.debug and not ctx.json_output:
            click.echo(
                f"Using project: {selected_project.name}{selected_project.get_quota_status()}"
            )

        try:
            submission = job_submit.submit_training_job(
                api,
                config=config,
                name=name,
                command=command,
                quota=resolved_quota,
                framework="pytorch",
                project_id=project_id,
                workspace_id=selected_workspace_id,
                image=image,
                priority=priority,
                nodes=nodes,
                max_time_hours=max_time,
                project_name=selected_project.name,
            )
        except ValueError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return

        wrapped_command = submission.wrapped_command
        log_path = submission.log_path
        result = submission.result
        data = submission.data
        job_id = submission.job_id

        if not job_id:
            if ctx.json_output:
                click.echo(json_formatter.format_json(data if data else result))
            else:
                if isinstance(result, dict):
                    message = result.get("message") or "Job created (no job ID returned)"
                    click.echo(human_formatter.format_success(message))
                    if result.get("data") and ctx.debug:
                        click.echo(str(result["data"]))
                else:
                    click.echo(human_formatter.format_success("Job created"))
                    if ctx.debug:
                        click.echo(str(result))
            sys.exit(EXIT_SUCCESS)

        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
        else:
            click.echo(f"Job created: {job_id}")
            if ctx.debug:
                click.echo(f"Name: {name}")
                click.echo(f"Quota: {quota_spec.display()}")
                if nodes > 1:
                    click.echo(f"Nodes: {nodes}")
                if priority is not None:
                    click.echo(f"Priority: {priority}")
                click.echo(
                    f"Command: {wrapped_command[:80]}{'...' if len(wrapped_command) > 80 else ''}"
                )
                if log_path:
                    click.echo(f"Log file: {log_path}")
            elif priority is not None:
                click.echo(f"Priority: {priority}")
            click.echo(f"Check status with: inspire job status {job_id}")

        if watch:
            if ctx.json_output:
                sys.exit(EXIT_SUCCESS)

            if ctx.debug:
                click.echo("Following logs...")
            try:
                _exec_inspire_subcommand(["job", "logs", job_id, "--follow"])
            except Exception as e:
                click.echo(f"Failed to start log follow: {e}", err=True)
                click.echo(f"You can still run: inspire job logs {job_id} --follow")
                sys.exit(EXIT_GENERAL_ERROR)

        sys.exit(EXIT_SUCCESS)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_GENERAL_ERROR)


@click.command()
@click.argument("command")
@click.option(
    "--quota",
    "-q",
    required=True,
    help=(
        "Resource quota as 'gpu,cpu,mem' (mem in GiB). "
        "Example: '8,160,1800' for 8 GPUs + 160 CPU + 1800 GiB. "
        "See 'inspire resources specs' for valid triples; pass --group to disambiguate."
    ),
)
@click.option("--name", "-n", help="Job name (auto-generated if not specified)")
@click.option(
    "--watch",
    "-w",
    is_flag=True,
    help="Follow logs after the job is submitted",
)
@click.option(
    "--priority",
    type=int,
    default=None,
    help=(
        "Requested priority 1-10 (higher numbers request higher priority; "
        "project quota may cap it). Check `inspire job status` for priority_level."
    ),
)
@click.option(
    "--project",
    "-p",
    default=None,
    help="Project name (default from config [context].project; see 'inspire config context')",
)
@click.option("--workspace", help="Workspace name (from [workspaces])")
@click.option(
    "--group",
    default=None,
    help=(
        "Disambiguate to a specific compute group by name when --quota matches "
        "multiple groups. Partial matches accepted."
    ),
)
@click.option("--max-time", type=float, default=100.0, help="Max runtime in hours (default: 100)")
@click.option(
    "--image",
    default=None,
    help="Custom Docker image (default from config [job].image)",
)
@click.option(
    "--nodes", type=int, default=1, help="Number of nodes for multi-node training (default: 1)"
)
@pass_context
def run(
    ctx: Context,
    command: str,
    quota: str,
    name: str | None,
    watch: bool,
    priority: int | None,
    project: str | None,
    workspace: str | None,
    group: str | None,
    max_time: float,
    image: str | None,
    nodes: int,
) -> None:
    """Quick job submission via quota triple.

    \b
    Examples:
        inspire run "python train.py" -q 8,160,1800
        inspire run "bash train.sh" -q 4,80,800 --group H100
        inspire run "python train.py" -q 1,20,200 --watch

    \b
    Priority:
        Requested priority is capped by the selected project quota. Use
        `inspire job status <name>` to inspect the platform-assigned
        priority_level.
    """
    _run_flow(
        ctx,
        command=command,
        quota=quota,
        name=name,
        watch=watch,
        priority=priority,
        project=project,
        workspace=workspace,
        group=group,
        max_time=max_time,
        image=image,
        nodes=nodes,
    )


__all__ = ["run"]
