"""Job create command."""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils import job_submit
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.quota_resolver import (
    QuotaMatchError,
    QuotaParseError,
    SCHEDULE_TYPE_TRAIN,
    parse_quota,
    resolve_quota,
)
from inspire.config import Config, ConfigError
from inspire.config.workload_profiles import apply_workload_profile, profile_required_message
from inspire.config.workspaces import select_workspace_id, workspace_label
from inspire.platform.web.session import get_web_session

def run_job_create(
    ctx: Context,
    *,
    name: str,
    quota: str | None,
    command: str,
    framework: str,
    priority: int | None,
    max_time: Optional[float],
    workspace: str | None,
    image: str | None,
    project: str | None,
    nodes: int | None,
    group: str | None,
    profile_name: str | None = None,
    dry_run: bool = False,
    auto_fault_tolerance: Optional[bool] = None,
    fault_tolerance_max_retry: Optional[int] = None,
    enable_notification: Optional[bool] = None,
    exclude_nodes: tuple[str, ...] | None = None,
    shm_size: Optional[int] = None,
) -> None:
    """Run the job creation flow."""
    try:
        config, _ = Config.from_files_and_env()

        fields = apply_workload_profile(
            profiles=getattr(config, "profiles", {}),
            kind="job",
            profile_name=profile_name,
            values={
                "workspace": workspace,
                "project": project,
                "group": group,
                "image": image,
                "quota": quota,
            },
        )
        workspace = fields["workspace"]
        project = fields["project"]
        group = fields["group"]
        image = fields["image"]
        quota = fields["quota"]

        if priority is None:
            priority = 10
        if auto_fault_tolerance is None:
            auto_fault_tolerance = config.job_auto_fault_tolerance
        if fault_tolerance_max_retry is None:
            fault_tolerance_max_retry = config.job_fault_tolerance_max_retry
        if enable_notification is None:
            enable_notification = config.job_enable_notification

        if not group:
            _handle_error(
                ctx,
                "ValidationError",
                profile_required_message("job", "group"),
                EXIT_CONFIG_ERROR,
            )
            return
        if not image:
            _handle_error(
                ctx,
                "ValidationError",
                profile_required_message("job", "image"),
                EXIT_CONFIG_ERROR,
            )
            return
        if not project:
            _handle_error(
                ctx,
                "ValidationError",
                profile_required_message("job", "project"),
                EXIT_CONFIG_ERROR,
            )
            return
        if not workspace:
            _handle_error(
                ctx,
                "ValidationError",
                profile_required_message("job", "workspace"),
                EXIT_CONFIG_ERROR,
            )
            return
        if not quota:
            _handle_error(
                ctx,
                "ValidationError",
                profile_required_message("job", "quota"),
                EXIT_CONFIG_ERROR,
            )
            return
        if nodes is None:
            nodes = 1

        try:
            quota_spec = parse_quota(quota)
        except QuotaParseError as e:
            _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
            return

        session = get_web_session()
        selected_workspace_id = select_workspace_id(
            config,
            explicit_workspace_name=workspace,
            session=session,
        )
        if not selected_workspace_id:
            from inspire.config.workspaces import workspace_required_hint

            _handle_error(
                ctx,
                "ConfigError",
                f"{profile_required_message('job', 'workspace')} {workspace_required_hint(config)}.",
                EXIT_CONFIG_ERROR,
            )
            return

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
                            f"(max for project '{scrub_raw_ids(selected.name)}')"
                        )
                    priority = max_priority
            except ValueError:
                pass

        if not ctx.json_output:
            if fallback_msg:
                click.echo(scrub_raw_ids(fallback_msg))
            click.echo(
                f"Using project: {scrub_raw_ids(selected.name)}"
                f"{scrub_raw_ids(selected.get_quota_status())}"
            )
            click.echo(
                f"Using compute group: {scrub_raw_ids(resolved_quota.compute_group_name)} "
                f"({resolved_quota.gpu_count}x{resolved_quota.gpu_type or 'CPU'}, "
                f"{resolved_quota.cpu_count} CPU, {resolved_quota.memory_gib} GiB)"
            )

        try:
            plan = job_submit.build_training_job_plan(
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
                auto_fault_tolerance=auto_fault_tolerance,
                fault_tolerance_max_retry=fault_tolerance_max_retry,
                enable_notification=enable_notification,
                exclude_nodes=exclude_nodes,
                shm_size=shm_size,
            )
        except ValueError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return

        plan_exclude_nodes = job_submit.training_plan_exclude_nodes(plan)

        if dry_run:
            if ctx.json_output:
                click.echo(json_formatter.format_json(job_submit.training_plan_payload(plan)))
                return
            click.echo(human_formatter.format_success(f"Dry run: job create plan for {name}"))
            click.echo(f"Project: {scrub_raw_ids(selected.name)}")
            click.echo(
                f"Workspace: {scrub_raw_ids(workspace_label(session, selected_workspace_id, workspace))}"
            )
            click.echo(f"Compute: {scrub_raw_ids(resolved_quota.compute_group_name)}")
            click.echo(f"Quota: {quota_spec.display()}")
            if priority is not None:
                click.echo(f"Priority: {priority}")
            if nodes > 1:
                click.echo(f"Nodes: {nodes}")
            if enable_notification:
                click.echo("Status notifications: enabled")
            if plan.shm_size_gib is not None:
                click.echo(f"Shared memory: {plan.shm_size_gib} GiB")
            if plan_exclude_nodes:
                click.echo(f"Exclude nodes: {scrub_raw_ids(', '.join(plan_exclude_nodes))}")
            click.echo(f"Image: {scrub_raw_ids(image)}")
            click.echo(f"Command: {scrub_raw_ids(plan.wrapped_command)}")
            if plan.log_path:
                click.echo(f"Log file: {scrub_raw_ids(plan.log_path)}")
            click.echo("No job was submitted.")
            return

        submission = job_submit.submit_training_job(
            session=session,
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
            auto_fault_tolerance=auto_fault_tolerance,
            fault_tolerance_max_retry=fault_tolerance_max_retry,
            enable_notification=enable_notification,
            exclude_nodes=exclude_nodes,
            shm_size=shm_size,
        )

        wrapped_command = submission.wrapped_command
        log_path = submission.log_path
        result = submission.result

        data = submission.data
        job_id = submission.job_id

        if ctx.json_output:
            payload = dict(data if data else result)
            payload.setdefault("name", name)
            payload.setdefault("enable_notification", bool(enable_notification))
            if plan.shm_size_gib is not None:
                payload.setdefault("shm_size_gib", plan.shm_size_gib)
            click.echo(json_formatter.format_json(payload))
            return

        if job_id:
            click.echo(human_formatter.format_success(f"Job created: {name}"))
            click.echo(f"Quota: {quota_spec.display()}")
            if priority is not None:
                click.echo(f"Priority: {priority}")
            if nodes > 1:
                click.echo(f"Nodes:    {nodes}")
            if enable_notification:
                click.echo("Status notifications: enabled")
            if plan.shm_size_gib is not None:
                click.echo(f"Shared memory: {plan.shm_size_gib} GiB")
            if plan_exclude_nodes:
                click.echo(f"Exclude nodes: {scrub_raw_ids(', '.join(plan_exclude_nodes))}")
            if auto_fault_tolerance:
                click.echo(f"Fault tolerance: enabled (max retry: {fault_tolerance_max_retry or 10})")
            max_cmd_len = 80
            if len(wrapped_command) > max_cmd_len:
                display_cmd = wrapped_command[:max_cmd_len]
                suffix = " ... (truncated)"
            else:
                display_cmd = wrapped_command
                suffix = ""
            click.echo(f"Command:  {scrub_raw_ids(display_cmd)}{suffix}")
            if log_path:
                click.echo(f"Log file:  {scrub_raw_ids(log_path)}")
            click.echo(
                "\nCheck status with: "
                f"inspire job status {scrub_raw_ids(name)} --workspace {scrub_raw_ids(workspace)}"
            )
            return

        if isinstance(result, dict):
            message = result.get("message") or f"Job created: {name}"
            click.echo(human_formatter.format_success(message))
        else:
            click.echo(human_formatter.format_success(f"Job created: {name}"))
        click.echo(
            "Check status with: "
            f"inspire job status {scrub_raw_ids(name)} --workspace {scrub_raw_ids(workspace)}"
        )

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("create")
@click.option("--name", "-n", required=True, help="Job name")
@click.option(
    "--quota",
    "-q",
    help=(
        "Resource quota as 'gpu,cpu,mem' (mem in GiB). "
        "Example: '4,80,800' for 4 GPU + 80 CPU + 800 GiB. "
        "The triple must match a quota row in the workspace (see 'inspire job quota'); "
        "pass --group <full compute group name> to disambiguate."
    ),
)
@click.option("--command", "-c", required=True, help="Start command")
@click.option(
    "--framework",
    default="pytorch",
    help=(
        "Training framework label shown by the platform (default: pytorch). "
        "This does not choose the Docker image; use --image for that. "
        "Most users should keep the default."
    ),
)
@click.option(
    "--priority",
    type=click.IntRange(1, 10),
    default=10,
    show_default=True,
    help=(
        "Task priority 1-10 (1-3=LOW preemptible, 4=NORMAL, 5-10=HIGH stable). "
        "The selected project's platform policy may cap the requested value. "
        "Check `inspire job status <name> --workspace <workspace>` for the resolved priority_level."
    ),
)
@click.option(
    "--auto-fault-tolerance/--no-auto-fault-tolerance",
    "auto_fault_tolerance",
    default=None,
    help=(
        "Ask the platform to auto-restart the training job after failures. "
        "Default from config [job].auto_fault_tolerance, or False."
    ),
)
@click.option(
    "--fault-tolerance-max-retry",
    type=click.IntRange(min=1),
    default=None,
    help=(
        "Max platform restart attempts when --auto-fault-tolerance is enabled "
        "(default 10, or config [job].fault_tolerance_max_retry). Ignored when "
        "fault tolerance is off."
    ),
)
@click.option(
    "--enable-notification/--no-enable-notification",
    default=None,
    help=(
        "Enable Feishu status notifications for this job. The platform sends "
        "updates to the current user's bound Feishu account. Default from "
        "INSPIRE_JOB_ENABLE_NOTIFICATION or [job].enable_notification; otherwise False."
    ),
)
@click.option(
    "--max-time",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help=(
        "Max runtime in hours. Omit for no time limit (the platform will not "
        "kill the job on a timer). The platform caps this at ~24 days when set."
    ),
)
@click.option("--workspace", help="Workspace name. Required unless supplied by --profile.")
@click.option(
    "--profile",
    "profile_name",
    default=None,
    help="Job condition profile providing workspace/project/group/quota/image.",
)
@click.option(
    "--group",
    help=(
        "Full compute group name copied from the same quota row as --quota. "
        "Required unless supplied by --profile. "
        "Partial matches are not accepted."
    ),
)
@click.option(
    "--image",
    help="Docker image URL or visible image name. Required unless supplied by --profile.",
)
@click.option(
    "--project",
    "-p",
    help="Project name. Required unless supplied by --profile.",
)
@click.option(
    "--nodes",
    type=click.IntRange(1),
    default=1,
    show_default=True,
    help="Number of nodes for multi-node training.",
)
@click.option(
    "--exclude-node",
    "exclude_nodes",
    multiple=True,
    metavar="NODE_NAME",
    help=(
        "Exclude a Ready node from placement for this job. Repeat for multiple nodes. "
        "This is not node pinning."
    ),
)
@click.option(
    "--shm-size",
    type=click.IntRange(min=1),
    default=None,
    help=(
        "Per-instance shared memory in GiB. Must be <= quota mem. "
        "Overrides INSPIRE_SHM_SIZE/job.shm_size and maps to platform shm_gi."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Resolve workspace, project, quota, compute group, image, and final command, "
        "then print the plan without submitting the job."
    ),
)
@pass_context
def create(
    ctx: Context,
    name: str,
    quota: str,
    command: str,
    framework: str,
    priority: Optional[int],
    auto_fault_tolerance: Optional[bool],
    fault_tolerance_max_retry: Optional[int],
    enable_notification: Optional[bool],
    max_time: Optional[float],
    workspace: Optional[str],
    profile_name: Optional[str],
    group: Optional[str],
    image: Optional[str],
    project: Optional[str],
    nodes: Optional[int],
    exclude_nodes: tuple[str, ...],
    shm_size: Optional[int],
    dry_run: bool,
) -> None:
    """Create a GPU batch job.

    Use this for fixed-size GPU work: single-node training, multi-node
    distributed training, batch inference, or a fixed GPU worker pool.
    If the ``me`` path alias is configured, stdout/stderr are captured under
    ``me/.inspire`` so ``inspire job logs`` can read them later through a
    notebook connection with access to the same shared storage.

    \b
    Examples:
        inspire job create -n pr-123 --workspace 分布式训练空间 --project CI-情境智能 \
          --group H200-2号机房 -q 4,80,800 --image sandbox-base:latest --nodes 1 \
          -c "bash repo/train.sh"
        inspire job create -n test --workspace 分布式训练空间 --project CI-情境智能 \
          --group H200-2号机房 -q 1,20,200 --image sandbox-base:latest --nodes 1 \
          -c "python train.py" --priority 9

    \b
    Priority:
        The selected project's platform policy may cap the requested priority.
        Use `inspire job status <name> --workspace <workspace>` to inspect the platform-assigned
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
        profile_name=profile_name,
        dry_run=dry_run,
        auto_fault_tolerance=auto_fault_tolerance,
        fault_tolerance_max_retry=fault_tolerance_max_retry,
        enable_notification=enable_notification,
        exclude_nodes=exclude_nodes,
        shm_size=shm_size,
    )
