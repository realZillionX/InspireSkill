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
from inspire.cli.utils.compute_group_autoselect import find_best_compute_group_location
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id


def run_job_create(
    ctx: Context,
    *,
    name: str,
    resource: str,
    command: str,
    framework: str,
    priority: int | None,
    max_time: float,
    location: str,
    workspace: str | None,
    auto: bool,
    image: str | None,
    project: str | None,
    nodes: int,
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
            requested_gpu_type, requested_gpu_count = api.resource_manager.parse_resource_request(
                resource
            )
        except Exception as e:
            _handle_error(
                ctx,
                "ValidationError",
                f"Invalid resource spec: {e}",
                EXIT_VALIDATION_ERROR,
            )
            return

        selected_workspace_id = select_workspace_id(
            config,
            gpu_type=requested_gpu_type.value,
            explicit_workspace_name=workspace,
        )
        if not selected_workspace_id:
            _handle_error(
                ctx,
                "ConfigError",
                "No workspace_id configured for GPU workloads. "
                "Set [workspaces].gpu or INSPIRE_WORKSPACE_ID.",
                EXIT_CONFIG_ERROR,
            )
            return

        if auto and not location:
            best, selected_location, selected_group_name = find_best_compute_group_location(
                api,
                gpu_type=requested_gpu_type.value,
                min_gpus=requested_gpu_count,
                include_preemptible=True,
                instance_count=nodes,
            )

            if not best:
                _handle_error(
                    ctx,
                    "InsufficientResources",
                    f"No {requested_gpu_type.value} compute group has at least {requested_gpu_count} available GPUs",
                    EXIT_VALIDATION_ERROR,
                )
                return

            location = selected_location or selected_group_name

            if not ctx.json_output:
                if getattr(best, "selection_source", "") == "nodes" and getattr(
                    best, "free_nodes", 0
                ):
                    click.echo(
                        "Auto-selected: "
                        f"{selected_group_name}, {best.free_nodes} full nodes free "
                        f"({best.available_gpus} GPUs)"
                    )
                else:
                    preempt_note = (
                        f" (+{best.low_priority_gpus} preemptible)"
                        if getattr(best, "low_priority_gpus", 0) > 0
                        else ""
                    )
                    click.echo(
                        f"Auto-selected: {selected_group_name}, "
                        f"{best.available_gpus} GPUs available{preempt_note}"
                    )

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

        # Cap priority to the selected project's max priority.
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

        try:
            submission = job_submit.submit_training_job(
                api,
                config=config,
                name=name,
                command=command,
                resource=resource,
                framework=framework,
                location=location,
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
            click.echo(human_formatter.format_success(f"Job created: {job_id}"))
            click.echo(f"\nName:     {name}")
            click.echo(f"Resource: {resource}")
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
            click.echo(f"\nCheck status with: inspire job status {job_id}")
            return

        if isinstance(result, dict):
            message = result.get("message") or "Job created (no job ID returned)"
            click.echo(human_formatter.format_success(message))
            if result.get("data"):
                click.echo(str(result["data"]))
        else:
            click.echo(human_formatter.format_success("Job created (no job ID returned)"))
            click.echo(str(result))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("create")
@click.option("--name", "-n", required=True, help="Job name")
@click.option("--resource", "-r", required=True, help="Resource spec (e.g., '4xH200')")
@click.option("--command", "-c", required=True, help="Start command")
@click.option("--framework", default="pytorch", help="Training framework (default: pytorch)")
@click.option(
    "--priority",
    type=int,
    default=None,
    help=(
        "Requested priority 1-10 (higher numbers request higher priority; "
        "project quota may cap it). Check `inspire job status` for priority_level."
    ),
)
@click.option("--max-time", type=float, default=100.0, help="Max runtime in hours (default: 100)")
@click.option("--location", help="Preferred datacenter location")
@click.option("--workspace", help="Workspace name (from [workspaces])")
@click.option(
    "--auto/--no-auto",
    default=True,
    help="Auto-select best location based on node availability (default: auto)",
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
    help="Project name or ID (default from config [context].project or [job].project_id)",
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
    resource: str,
    command: str,
    framework: str,
    priority: Optional[int],
    max_time: float,
    location: str,
    workspace: Optional[str],
    auto: bool,
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
        inspire job create --name "pr-123" --resource "4xH200" --command "cd /path/to/code && bash train.sh"
        inspire job create -n test -r H200 -c "python train.py" --priority 9
        inspire job create -n test -r 4xH200 -c "python train.py" --no-auto

    \b
    Priority:
        Requested priority is capped by the selected project quota. Use
        `inspire job status <job-id>` to inspect the platform-assigned
        priority_level.
    """
    run_job_create(
        ctx,
        name=name,
        resource=resource,
        command=command,
        framework=framework,
        priority=priority,
        max_time=max_time,
        location=location,
        workspace=workspace,
        auto=auto,
        image=image,
        project=project,
        nodes=nodes,
    )
