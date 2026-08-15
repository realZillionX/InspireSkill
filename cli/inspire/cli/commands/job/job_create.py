"""Job create command."""

from __future__ import annotations

from typing import Optional, Sequence

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
from inspire.cli.utils.dataset_mounts import (
    DatasetSpecError,
    dataset_mount_views,
    dataset_option,
    describe_dataset_mounts,
    parse_dataset_specs_or_usage_error,
    resolve_dataset_info,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import reject_id_at_boundary, remember_resource_identity
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.task_priority import (
    TaskPriorityError,
    resolve_task_priority,
    task_priority_option,
)
from inspire.cli.utils.quota_resolver import (
    QuotaMatchError,
    QuotaParseError,
    SCHEDULE_TYPE_TRAIN,
    ensure_priority_allowed,
    parse_quota,
    resolve_quota,
)
from inspire.config import Config, ConfigError
from inspire.config.workload_profiles import apply_workload_profile, profile_required_message
from inspire.config.workspaces import select_workspace_id, workspace_label
from inspire.platform.web.browser_api import DatasetMount
from inspire.platform.web.session import get_web_session
from inspire.platform.web.browser_api.workspaces import is_fair_scheduling_workspace

DEFAULT_FRAMEWORK = "pytorch"


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
    fault_tolerance_retry_interval: Optional[int] = None,
    enable_notification: Optional[bool] = None,
    exclude_nodes: tuple[str, ...] | None = None,
    shm_size: Optional[int] = None,
    dataset_mounts: Sequence[DatasetMount] = (),
    envs: Optional[list[dict[str, str]]] = None,
    description: Optional[str] = None,
    keep_after_success: Optional[float] = None,
    keep_after_failure: Optional[float] = None,
    public_path_readonly: Optional[bool] = None,
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
        project = reject_id_at_boundary(
            ctx,
            project,
            resource_type="project",
            list_command="inspire project list",
        )
        if nodes is None:
            nodes = 1

        try:
            quota_spec = parse_quota(quota)
        except QuotaParseError as e:
            _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
            return

        session = get_web_session()
        selected_workspace_id = select_workspace_id(
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
            selected, _selection_message = job_submit.select_project_for_workspace(
                config,
                workspace_id=selected_workspace_id,
                requested=project,
            )
        except ValueError as e:
            _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
            return

        selected_project_id = selected.project_id
        fair_scheduling = is_fair_scheduling_workspace(session, selected_workspace_id)
        priority = resolve_task_priority(
            priority,
            fair_scheduling=fair_scheduling,
            project_limit=selected.priority_name,
        )
        try:
            ensure_priority_allowed(
                resolved_quota, priority, quota_command="inspire job quota"
            )
        except QuotaMatchError as err:
            _handle_error(ctx, "ValidationError", str(err), EXIT_VALIDATION_ERROR)
            return

        # The platform resolves and checks every mount before the job is
        # submitted, exactly as the console's 校验数据 button does.
        try:
            dataset_info = resolve_dataset_info(
                dataset_mounts,
                workspace_id=selected_workspace_id,
                session=session,
            )
        except DatasetSpecError as e:
            _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
            return

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
                dataset_info=dataset_info,
                envs=envs,
                description=description,
                keep_after_success_hours=keep_after_success,
                keep_after_failure_hours=keep_after_failure,
                public_path_readonly=public_path_readonly,
                fault_tolerance_retry_interval_sec=fault_tolerance_retry_interval,
                session=session,
            )
        except ValueError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return

        plan_exclude_nodes = job_submit.training_plan_exclude_nodes(plan)

        if dry_run:
            workspace_text = workspace_label(
                session,
                selected_workspace_id,
                workspace,
            )
            if ctx.json_output:
                dry_run_payload: dict[str, object] = {
                    "dry_run": True,
                    "name": name,
                    "workspace": workspace_text,
                    "project": selected.name,
                    "compute_group": resolved_quota.compute_group_name,
                    "resource": {
                        "gpu": resolved_quota.gpu_count,
                        "cpu": resolved_quota.cpu_count,
                        "memory_gib": resolved_quota.memory_gib,
                    },
                    "priority": priority,
                    "nodes": nodes,
                    "framework": framework,
                    "enable_notification": bool(enable_notification),
                    # Every field the plan submits has to appear here, or a
                    # dry-run cannot be used to check what will be submitted.
                    "auto_fault_tolerance": bool(auto_fault_tolerance),
                    "max_time_hours": max_time,
                    "image": scrub_raw_ids(image),
                    "command": scrub_raw_ids(plan.wrapped_command),
                    "shared_memory_gib": plan.shm_size_gib,
                }
                if auto_fault_tolerance:
                    dry_run_payload["fault_tolerance_max_retry"] = fault_tolerance_max_retry
                    if fault_tolerance_retry_interval is not None:
                        dry_run_payload["fault_tolerance_retry_interval_seconds"] = (
                            fault_tolerance_retry_interval
                        )
                if resolved_quota.gpu_type:
                    resource = dry_run_payload["resource"]
                    assert isinstance(resource, dict)
                    resource["gpu_type"] = resolved_quota.gpu_type
                if plan_exclude_nodes:
                    dry_run_payload["exclude_nodes"] = [
                        scrub_raw_ids(node) for node in plan_exclude_nodes
                    ]
                if dataset_mounts:
                    dry_run_payload["datasets"] = dataset_mount_views(dataset_mounts)
                if envs:
                    dry_run_payload["env"] = [entry["name"] for entry in envs]
                if description is not None:
                    dry_run_payload["description"] = scrub_raw_ids(description)
                if keep_after_success is not None:
                    dry_run_payload["keep_after_success_hours"] = keep_after_success
                if keep_after_failure is not None:
                    dry_run_payload["keep_after_failure_hours"] = keep_after_failure
                if public_path_readonly is not None:
                    dry_run_payload["public_path_readonly"] = bool(public_path_readonly)
                click.echo(json_formatter.format_json(dry_run_payload))
                return
            click.echo(f"Create plan: {scrub_raw_ids(name)}")
            click.echo(f"Project: {scrub_raw_ids(selected.name)}")
            click.echo(f"Workspace: {scrub_raw_ids(workspace_text)}")
            click.echo(f"Compute: {scrub_raw_ids(resolved_quota.compute_group_name)}")
            click.echo(f"Resource: {quota_spec.display()}")
            if priority is not None:
                click.echo(f"Priority: {priority}")
            if nodes > 1:
                click.echo(f"Nodes: {nodes}")
            if framework and framework != DEFAULT_FRAMEWORK:
                click.echo(f"Framework: {scrub_raw_ids(framework)}")
            if enable_notification:
                click.echo("Notifications: enabled")
            if max_time is not None:
                click.echo(f"Max runtime: {max_time} h")
            if auto_fault_tolerance:
                retry_line = f"Fault tolerance: enabled, max {fault_tolerance_max_retry} retries"
                if fault_tolerance_retry_interval is not None:
                    retry_line += f", {fault_tolerance_retry_interval}s apart"
                click.echo(retry_line)
            if plan.shm_size_gib is not None:
                click.echo(f"Shared memory: {plan.shm_size_gib} GiB")
            if plan_exclude_nodes:
                click.echo(f"Exclude nodes: {scrub_raw_ids(', '.join(plan_exclude_nodes))}")
            for line in describe_dataset_mounts(dataset_mounts):
                click.echo(f"Dataset: {line}")
            if envs:
                # Names only: a value can be a token, and a plan is printed.
                click.echo(f"Env: {', '.join(entry['name'] for entry in envs)}")
            if description is not None:
                click.echo(f"Description: {scrub_raw_ids(description)}")
            if keep_after_success is not None:
                click.echo(f"Keep after success: {keep_after_success} h")
            if keep_after_failure is not None:
                click.echo(f"Keep after failure: {keep_after_failure} h")
            if public_path_readonly is not None:
                click.echo(
                    "Public path: read-only" if public_path_readonly else "Public path: writable"
                )
            click.echo(f"Image: {scrub_raw_ids(image)}")
            click.echo(f"Command: {scrub_raw_ids(plan.wrapped_command)}")
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
            dataset_info=dataset_info,
            envs=envs,
            description=description,
            keep_after_success_hours=keep_after_success,
            keep_after_failure_hours=keep_after_failure,
            public_path_readonly=public_path_readonly,
            fault_tolerance_retry_interval_sec=fault_tolerance_retry_interval,
        )

        data = submission.data
        job_id = submission.job_id
        if job_id:
            remember_resource_identity(
                session=session,
                resource_type="job",
                resource_id=job_id,
                name=name,
                workspace_id=selected_workspace_id,
                owner_scope="self",
                status=str(data.get("status") or "") if isinstance(data, dict) else "",
            )

        if ctx.json_output:
            created: dict[str, object] = {"name": name, "status": "created"}
            if dataset_mounts:
                created["datasets"] = dataset_mount_views(dataset_mounts)
            click.echo(json_formatter.format_json(created))
            return

        click.echo(human_formatter.format_mutation_success("Job", "created", name))
        for line in describe_dataset_mounts(dataset_mounts):
            click.echo(f"Dataset: {line}")

    except TaskPriorityError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("create")
@click.option("--name", "-n", required=True, metavar="NAME", help="Job name")
@click.option("--command", "-c", required=True, help="Start command")
@click.option(
    "--workspace",
    metavar="NAME",
    help="Workspace name. Required unless supplied by --profile.",
)
@click.option(
    "--project",
    "-p",
    metavar="NAME",
    help="Project name. Required unless supplied by --profile.",
)
@click.option(
    "--group",
    metavar="NAME",
    help=(
        "Full compute group name copied from the same quota row as --quota. "
        "Required unless supplied by --profile. "
        "Partial matches are not accepted."
    ),
)
@click.option(
    "--quota",
    "-q",
    metavar="SPEC",
    help=(
        "Resource quota as 'gpu,cpu,mem' (mem in GiB). "
        "Example: '4,80,800' for 4 GPU + 80 CPU + 800 GiB. "
        "The triple must match a quota row in the workspace (see 'inspire job quota'); "
        "pass --group <full compute group name> to disambiguate."
    ),
)
@click.option(
    "--image",
    "-i",
    metavar="NAME|URL",
    help="Docker image URL or visible image name. Required unless supplied by --profile.",
)
@click.option(
    "--profile",
    "profile_name",
    default=None,
    metavar="NAME",
    help="Job condition profile providing workspace/project/group/quota/image.",
)
@click.option(
    "--framework",
    default=DEFAULT_FRAMEWORK,
    help=(
        "Training framework label shown by the platform (default: pytorch). "
        "This does not choose the Docker image; use --image for that. "
        "Most users should keep the default."
    ),
)
@task_priority_option()
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
    "--fault-tolerance-retry-interval",
    type=click.IntRange(min=1),
    default=None,
    metavar="SECONDS",
    help=(
        "Seconds the platform waits between restart attempts. Requires "
        "--auto-fault-tolerance. Omit to leave the platform default."
    ),
)
@dataset_option()
@click.option(
    "--env",
    "env_values",
    multiple=True,
    metavar="KEY=VALUE",
    help=(
        "Set an environment variable in every instance of this job "
        "(repeatable). The platform injects it into the container, so the "
        "command no longer has to carry the assignment inline."
    ),
)
@click.option(
    "--description",
    default=None,
    metavar="TEXT",
    help="Free-text description stored with the job on the platform.",
)
@click.option(
    "--keep-after-success",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    metavar="HOURS",
    help=(
        "Keep the containers alive this many hours after the job succeeds, so "
        "they can still be inspected with 'inspire job shell'. Omit to let the "
        "platform release them as usual."
    ),
)
@click.option(
    "--keep-after-failure",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    metavar="HOURS",
    help=(
        "Keep the containers alive this many hours after the job fails. Omit "
        "to let the platform release them as usual."
    ),
)
@click.option(
    "--public-path-readonly/--no-public-path-readonly",
    default=None,
    help=(
        "Mount the project's public path read-only inside the containers "
        "(平台 高级设置·项目Public只读挂载). Omit to leave the platform default."
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
    fault_tolerance_retry_interval: Optional[int],
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
    datasets: tuple[str, ...],
    env_values: tuple[str, ...],
    description: Optional[str],
    keep_after_success: Optional[float],
    keep_after_failure: Optional[float],
    public_path_readonly: Optional[bool],
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
          -c "python train.py" --priority 4
        inspire job create -n eval --workspace 分布式训练空间 --project CI-情境智能 \
          --group H200-2号机房 -q 1,20,200 --image sandbox-base:latest --nodes 1 \
          --dataset pixabay-81k:v0 --env WANDB_MODE=offline --keep-after-failure 1 \
          -c "python eval.py --data /inspire/dataset/pixabay-81k/v0"

    \b
    Priority:
        The selected project's platform policy may cap the requested priority.
        Use `inspire job status <name> --workspace <workspace>` to inspect the platform-assigned
        priority_level.
    """
    dataset_mounts = parse_dataset_specs_or_usage_error(datasets)
    try:
        envs = job_submit.parse_env_assignments(env_values)
    except ValueError as e:
        raise click.UsageError(str(e)) from e

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
        fault_tolerance_retry_interval=fault_tolerance_retry_interval,
        enable_notification=enable_notification,
        exclude_nodes=exclude_nodes,
        shm_size=shm_size,
        dataset_mounts=dataset_mounts,
        envs=envs,
        description=description,
        keep_after_success=keep_after_success,
        keep_after_failure=keep_after_failure,
        public_path_readonly=public_path_readonly,
    )
