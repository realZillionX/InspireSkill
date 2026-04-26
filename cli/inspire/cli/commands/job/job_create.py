"""Job create command."""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
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


def run_job_create(
    ctx: Context,
    *,
    name: str,
    quota: str,
    command: str,
    framework: str,
    priority: int | None,
    max_time: float,
    workspace: str | None,
    image: str | None,
    project: str | None,
    nodes: int,
    group: str | None,
) -> None:
    """Run the job creation flow."""
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

        try:
            selected, fallback_msg = job_submit.select_project_for_workspace(
                config,
                workspace_id=selected_workspace_id,
                requested=project,
            )
        except ValueError as e:
            error_type = "QuotaExceeded" if "over quota" in str(e) else "ValidationError"
            _handle_error(ctx, error_type, str(e), EXIT_CONFIG_ERROR)
            return

        selected_project_id = selected.project_id

        if selected.priority_name:
            try:
                max_priority = int(selected.priority_name)
                if priority is not None and priority > max_priority:
                    if not ctx.json_output:
                        click.echo(
                            f"Capping priority {priority} → {max_priority} "
                            f"(max for project '{selected.name}')"
                        )
                    priority = max_priority
            except ValueError:
                pass

        if not ctx.json_output:
            if fallback_msg:
                click.echo(fallback_msg)
            click.echo(f"Using project: {selected.name}{selected.get_quota_status()}")
            click.echo(
                f"Using compute group: {resolved_quota.compute_group_name} "
                f"({resolved_quota.gpu_count}x{resolved_quota.gpu_type or 'CPU'}, "
                f"{resolved_quota.cpu_count} CPU, {resolved_quota.memory_gib} GiB)"
            )

        try:
            submission = job_submit.submit_training_job(
                api,
                config=config,
                name=name,
                command=command,
                quota=resolved_quota,
                framework=framework,
                project_id=selected_project_id,
                workspace_id=selected_workspace_id,
                image=image,
                priority=priority,
                nodes=nodes,
                max_time_hours=max_time,
                project_name=selected.name,
            )
        except ValueError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return

        wrapped_command = submission.wrapped_command
        log_path = submission.log_path
        result = submission.result

        data = submission.data
        job_id = submission.job_id

        if ctx.json_output:
            payload = data if data else result
            click.echo(json_formatter.format_json(payload))
            return

        if job_id:
            click.echo(human_formatter.format_success(f"Job created: {name}"))
            click.echo(f"Quota: {quota_spec.display()}")
            if priority is not None:
                click.echo(f"Priority: {priority}")
            if nodes > 1:
                click.echo(f"Nodes:    {nodes}")
            max_cmd_len = 80
            if len(wrapped_command) > max_cmd_len:
                display_cmd = wrapped_command[:max_cmd_len]
                suffix = " ... (truncated)"
            else:
                display_cmd = wrapped_command
                suffix = ""
            click.echo(f"Command:  {display_cmd}{suffix}")
            if log_path:
                click.echo(f"Log file:  {log_path}")
            click.echo(f"\nCheck status with: inspire job status {name}")
            return

        if isinstance(result, dict):
            message = result.get("message") or f"Job created: {name}"
            click.echo(human_formatter.format_success(message))
            if result.get("data"):
                click.echo(str(result["data"]))
        else:
            click.echo(human_formatter.format_success(f"Job created: {name}"))
            click.echo(str(result))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("create")
@click.option("--name", "-n", required=True, help="Job name")
@click.option(
    "--quota",
    "-q",
    required=True,
    help=(
        "Resource quota as 'gpu,cpu,mem' (mem in GiB). "
        "Example: '4,80,800' for 4 GPU + 80 CPU + 800 GiB. "
        "The triple must match a quota_id in the workspace (see 'inspire resources specs'); "
        "pass --group to disambiguate when multiple compute groups offer the same triple."
    ),
)
@click.option("--command", "-c", required=True, help="Start command")
@click.option("--framework", default="pytorch", help="Training framework (default: pytorch)")
@click.option(
    "--priority",
    type=click.IntRange(1, 10),
    default=None,
    help=(
        "Task priority 1-10 (1-3=LOW preemptible, 4=NORMAL, 5-10=HIGH stable). "
        "Project quota may cap the requested value. Check `inspire job status` "
        "for the resolved priority_level."
    ),
)
@click.option("--max-time", type=float, default=100.0, help="Max runtime in hours (default: 100)")
@click.option("--workspace", help="Workspace name (from [workspaces])")
@click.option(
    "--group",
    default=None,
    help=(
        "Disambiguate to a specific compute group by name when the --quota triple "
        "matches multiple groups. Partial matches accepted."
    ),
)
@click.option(
    "--image",
    default=None,
    help="Custom Docker image (default from config [job].image)",
)
@click.option(
    "--project",
    "-p",
    default=None,
    help="Project name (default from config [context].project; see 'inspire config context')",
)
@click.option(
    "--nodes",
    type=int,
    default=1,
    help="Number of nodes for multi-node training (default: 1)",
)
@pass_context
def create(
    ctx: Context,
    name: str,
    quota: str,
    command: str,
    framework: str,
    priority: Optional[int],
    max_time: float,
    workspace: Optional[str],
    group: Optional[str],
    image: Optional[str],
    project: Optional[str],
    nodes: int,
) -> None:
    """Create a new training job.

    If ``INSPIRE_TARGET_DIR`` is configured, stdout/stderr are captured under that
    shared directory for later ``inspire job logs`` retrieval.

    \b
    Examples:
        export INSPIRE_TARGET_DIR="/train/logs"
        inspire job create -n pr-123 -q 4,80,800 -c "cd /path && bash train.sh"
        inspire job create -n test -q 1,20,200 -c "python train.py" --priority 9
        inspire job create -n test -q 4,80,800 -c "python train.py" --group H200

    \b
    Priority:
        Requested priority is capped by the selected project quota. Use
        `inspire job status <name>` to inspect the platform-assigned
        priority_level.
    """
    run_job_create(
        ctx,
        name=name,
        quota=quota,
        command=command,
        framework=framework,
        priority=priority,
        max_time=max_time,
        workspace=workspace,
        image=image,
        project=project,
        nodes=nodes,
        group=group,
    )
