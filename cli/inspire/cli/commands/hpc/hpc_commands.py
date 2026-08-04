"""HPC job commands for Inspire CLI."""

from __future__ import annotations

from typing import Any, Optional, cast

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
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.auth import AuthenticationError
from inspire.cli.utils.collection_output import (
    DEFAULT_COLLECTION_LIMIT,
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.task_priority import (
    TaskPriorityError,
    resolve_workspace_task_priority,
    task_priority_option,
)
from inspire.config import Config, ConfigError
from inspire.config.workload_profiles import apply_workload_profile, profile_required_message
from inspire.cli.utils.id_resolver import (
    forget_resource_identity,
    looks_like_platform_id,
    reject_id_at_boundary,
    remember_resource_identity,
    resolve_by_name,
    run_with_stale_handle_retry,
)
from inspire.cli.utils.project_resolver import (
    project_display_name,
    resolve_project_id as resolve_project_id_by_name,
)
from inspire.config.workspaces import select_workspace_id, workspace_label
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session

_DEFAULT_INSTANCE_SCAN_LIMIT = 500


def _current_user_id(session) -> str:  # noqa: ANN001
    me = browser_api_module.get_current_user(session=session)
    user_id = str(me.get("id") or me.get("user_id") or "").strip()
    if not user_id:
        raise ValueError("Cannot determine the current user from the live web session.")
    return user_id


def _created_hpc_job_id(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("job_id", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    for key in ("job", "data", "result"):
        value = _created_hpc_job_id(payload.get(key))
        if value:
            return value
    return ""


def _resolve_hpc_name_in_workspace(
    ctx: Context,
    *,
    config: Config,
    session,
    name: str,
    workspace: str,
    limit: int,
    pick: Optional[int] = None,
    require_live: bool = False,
) -> str:
    workspace_id = select_workspace_id(
        config,
        explicit_workspace_name=workspace,
        session=session,
    )
    if workspace_id is None:
        raise ConfigError("--workspace is required.")
    user_id = _current_user_id(session)

    def _lister():
        jobs, _ = browser_api_module.list_hpc_jobs(
            workspace_id=workspace_id,
            created_by=user_id,
            page_num=1,
            page_size=limit,
            session=session,
        )
        return [
            {
                "name": j.name,
                "id": j.job_id,
                "status": j.status,
                "workspace_id": j.workspace_id,
                "created_at": j.created_at,
            }
            for j in jobs
        ]

    return resolve_by_name(
        ctx,
        name=name,
        resource_type="hpc",
        list_candidates=_lister,
        json_output=ctx.json_output,
        pick_index=pick,
        session=session,
        workspace_id=workspace_id,
        owner_scope="self",
        require_live=require_live,
        list_command=f"inspire hpc list --workspace {workspace}",
    )


def _reject_hpc_name_at_boundary(ctx: Context, name: str) -> str:
    return reject_id_at_boundary(
        ctx,
        name,
        resource_type="hpc",
        list_command="inspire hpc list --workspace <workspace>",
    )


def _run_readonly_hpc_operation(
    ctx: Context,
    *,
    config: Config,
    session,
    name: str,
    workspace: str,
    limit: int,
    operation,
):
    """Run a read-only HPC operation and recover one stale cache hit."""
    def _resolve(require_live: bool) -> str:
        return _resolve_hpc_name_in_workspace(
            ctx,
            config=config,
            session=session,
            name=name,
            workspace=workspace,
            limit=limit,
            require_live=require_live,
        )

    def _invalidate(job_id: str) -> None:
        workspace_id = select_workspace_id(
            config,
            explicit_workspace_name=workspace,
            session=session,
        )
        if workspace_id is None:
            raise ConfigError("--workspace is required.")
        forget_resource_identity(
            session=session,
            resource_type="hpc",
            resource_id=job_id,
            workspace_id=workspace_id,
            owner_scope="self",
        )

    return run_with_stale_handle_retry(
        name=name,
        resolve_cached=lambda: _resolve(False),
        resolve_live=lambda _name: _resolve(True),
        operation=lambda job_id: operation(job_id, session),
        invalidate=_invalidate,
    )


def _resolve_project_id(
    config: Config,
    requested: Optional[str],
    *,
    workspace_id: Optional[str] = None,
    session=None,
    ctx: Context | None = None,
) -> str:
    """Resolve a visible project name against the current workspace."""
    if not requested:
        raise ConfigError("--project is required.")
    if ctx is not None:
        requested = reject_id_at_boundary(
            ctx,
            requested,
            resource_type="project",
            list_command="inspire project list",
        )
    elif looks_like_platform_id(requested):
        raise ConfigError("--project takes a project name.")
    if not workspace_id or session is None:
        raise ConfigError("A live workspace project list is required to resolve --project.")
    projects = browser_api_module.list_projects(
        workspace_id=workspace_id,
        session=session,
    )
    return resolve_project_id_by_name(config, requested, projects)


def _project_label(config: Config, project_id: str, requested: Optional[str]) -> str:
    if requested:
        return project_display_name(config, requested)
    return "(project name unavailable)"


def _looks_like_full_slurm_script(entrypoint: str) -> bool:
    stripped = entrypoint.lstrip()
    return stripped.startswith("#!") or "#SBATCH" in entrypoint


def _hpc_plan_payload(
    *,
    name: str,
    create_kwargs: dict[str, Any],
    project_label: str,
    workspace_label: str,
    compute_group_name: str,
) -> dict[str, Any]:
    sbatch = create_kwargs.get("sbatch_script") or {}
    cluster = create_kwargs.get("slurm_cluster_spec") or {}
    return {
        "dry_run": True,
        "name": name,
        "project_name": project_label,
        "workspace_name": workspace_label,
        "compute_group_name": compute_group_name,
        "image": cluster.get("image"),
        "image_type": cluster.get("image_type"),
        "instance_count": cluster.get("instance_count"),
        "resource": {
            "cpu": cluster.get("cpu"),
            "memory_gib": cluster.get("mem_gi"),
        },
        "entrypoint": sbatch.get("entrypoint"),
        "number_of_tasks": sbatch.get("number_of_tasks"),
        "cpus_per_task": sbatch.get("cpus_per_task"),
        "memory_per_cpu": sbatch.get("memory_per_cpu"),
        "enable_hyper_threading": sbatch.get("enable_hyper_threading"),
        "task_priority": create_kwargs.get("task_priority"),
    }


def build_hpc_create_payload(
    *,
    name: str,
    logic_compute_group_id: str,
    project_id: str,
    workspace_id: str,
    image: str,
    image_type: str,
    entrypoint: str,
    quota_id: str,
    instance_count: int,
    task_priority: int | None,
    number_of_tasks: int,
    cpus_per_task: int,
    memory_per_cpu: int,
    enable_hyper_threading: bool,
    resource_spec_price: dict[str, Any],
) -> dict[str, Any]:
    """Build the current Web UI v2 HPC create payload."""
    payload: dict[str, Any] = {
        "job_name": name,
        "logic_compute_group_id": logic_compute_group_id,
        "project_id": project_id,
        "workspace_id": workspace_id,
        "enable_notification": False,
        "sbatch_script": {
            "number_of_tasks": int(number_of_tasks),
            "cpus_per_task": int(cpus_per_task),
            "memory_per_cpu": f"{int(memory_per_cpu)}G",
            "enable_hyper_threading": bool(enable_hyper_threading),
            "entrypoint": entrypoint,
        },
        "slurm_cluster_spec": {
            "predef_quota_id": quota_id,
            "cpu": int(resource_spec_price.get("cpu_count") or 0),
            "mem_gi": int(resource_spec_price.get("memory_size_gib") or 0),
            "image": image,
            "image_type": image_type,
            "instance_count": int(instance_count),
            "spec_price": dict(resource_spec_price),
        },
    }
    if task_priority is not None:
        payload["task_priority"] = int(task_priority)
    return payload


def _format_hpc_list_rows(rows: list[dict[str, str]]) -> str:
    """Format HPC job rows into a compact name-first table."""
    if not rows:
        return "No HPC jobs found."

    table_rows = [(row["name"], row["status"], row["created_at"]) for row in rows]
    widths = [
        column_width("Name", [row[0] for row in table_rows], max_width=80),
        column_width("Status", [row[1] for row in table_rows], max_width=18),
        column_width("Created", [row[2] for row in table_rows], max_width=19),
    ]
    lines = [
        "HPC Jobs",
        *render_table(("Name", "Status", "Created"), table_rows, widths, line_char="─"),
    ]
    lines.append(f"Total: {len(rows)}")
    return "\n".join(lines)


_OUTPUT_METADATA_KEYS = {
    "debug",
    "method",
    "metadata",
    "payload",
    "progress",
    "raw",
    "request",
    "requestpayload",
    "response",
    "responsemetadata",
    "result",
    "scanned",
    "source",
}


def _is_output_id_key(key: object) -> bool:
    normalized = str(key or "").replace("-", "_").lower()
    return normalized in {"id", "ids"} or normalized.endswith("_id") or normalized.endswith("_ids")


def _public_output(value: object) -> Any:
    """Keep useful HPC results while hiding platform handles and metadata."""
    if isinstance(value, dict):
        return {
            key: _public_output(child)
            for key, child in value.items()
            if str(key or "").replace("-", "_").lower() not in _OUTPUT_METADATA_KEYS
            and not _is_output_id_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_public_output(item) for item in value]
    if isinstance(value, str):
        return scrub_raw_ids(value)
    return value


def _hpc_instance_name(inst: dict[str, Any], idx: int) -> str:
    for key in ("name", "instance_name", "pod_name", "component"):
        value = str(inst.get(key) or "").strip()
        if value:
            return scrub_raw_ids(value)
    return f"#{idx}"


def _format_hpc_instances(instances: list[dict[str, Any]]) -> str:
    """Format HPC pod/component instances as name-first rows."""
    if not instances:
        return "No HPC instances found."

    rendered = []
    for idx, inst in enumerate(instances, start=1):
        rendered.append(
            {
                "name": _hpc_instance_name(inst, idx),
                "status": scrub_raw_ids(inst.get("status") or inst.get("instance_status") or ""),
                "component": scrub_raw_ids(inst.get("component") or inst.get("type") or ""),
                "node": scrub_raw_ids(inst.get("node") or inst.get("node_name") or ""),
                "created": human_formatter.format_epoch(inst.get("created_at")),
            }
        )

    table_rows = [
        (row["name"], row["status"], row["component"], row["node"], row["created"])
        for row in rendered
    ]
    widths = [
        column_width("Instance", [row[0] for row in table_rows], max_width=64),
        column_width("Status", [row[1] for row in table_rows], max_width=18),
        column_width("Component", [row[2] for row in table_rows], max_width=18),
        column_width("Node", [row[3] for row in table_rows], max_width=32),
        column_width("Created", [row[4] for row in table_rows], max_width=19),
    ]
    lines = [
        "HPC Instances",
        *render_table(
            ("Instance", "Status", "Component", "Node", "Created"),
            table_rows,
            widths,
            line_char="─",
        ),
    ]
    lines.append(f"Total: {len(instances)} instance(s)")
    return "\n".join(lines)


def _fetch_hpc_instances(
    job_id: str,
    *,
    limit: int,
    session,
    show_all: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch the bounded instance page, expanding it only for explicit ``--all``."""
    rows, total = browser_api_module.list_hpc_job_instances(
        job_id,
        limit=limit,
        session=session,
    )
    if show_all and total > len(rows):
        expanded_rows, expanded_total = browser_api_module.list_hpc_job_instances(
            job_id,
            limit=max(total, len(rows), 1),
            session=session,
        )
        rows = expanded_rows
        total = max(total, expanded_total, len(rows))
    return rows, total


@click.command("list")
@click.option("--workspace", required=True, help="Workspace name")
@click.option("--status", "status_filter", default=None, help="Filter by HPC job status")
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum HPC jobs to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every HPC job.")
@pass_context
def list_hpc(
    ctx: Context,
    workspace: Optional[str],
    status_filter: Optional[str],
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List the current user's HPC jobs.

    \b
    Examples:
        inspire hpc list --workspace CPU资源空间 --status RUNNING
    """
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    request_limit = (
        effective_limit if effective_limit is not None else DEFAULT_COLLECTION_LIMIT
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        resolved_workspace_id = select_workspace_id(
            config,
            explicit_workspace_name=workspace,
            session=session,
        )
        created_by = _current_user_id(session)

        jobs, total = browser_api_module.list_hpc_jobs(
            workspace_id=resolved_workspace_id,
            created_by=created_by,
            status=status_filter,
            page_num=1,
            page_size=request_limit,
            session=session,
        )
        if show_all and total > len(jobs):
            jobs, expanded_total = browser_api_module.list_hpc_jobs(
                workspace_id=resolved_workspace_id,
                created_by=created_by,
                status=status_filter,
                page_num=1,
                page_size=max(total, len(jobs), 1),
                session=session,
            )
            total = max(total, expanded_total, len(jobs))
        page = bound_collection(jobs, limit=effective_limit, total=total)
        rows = [
            {
                "name": scrub_raw_ids(job.name or "N/A"),
                "status": scrub_raw_ids(job.status or "N/A"),
                "created_at": scrub_raw_ids(job.created_at or "N/A"),
                "entrypoint": scrub_raw_ids(job.entrypoint or ""),
                "project_name": scrub_raw_ids(job.project_name or ""),
                "compute_group_name": scrub_raw_ids(job.compute_group_name or ""),
            }
            for job in page.items
        ]

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "jobs": _public_output(rows),
                        **page.metadata(),
                    }
                )
            )
            return

        click.echo(_format_hpc_list_rows(rows))
        notice = truncation_notice(page, full_option="--all")
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("create")
@click.option("--name", "-n", required=True, help="HPC job name")
@click.option(
    "--entrypoint",
    "-c",
    required=True,
    help="Slurm script body (omit #SBATCH headers; use srun to launch the program)",
)
@click.option(
    "--group",
    "compute_group",
    help=(
        "Full compute group name copied from the same quota row as --quota. "
        "Required unless supplied by --profile "
        "(e.g. 'HPC-可上网区资源-2'; see 'inspire config context')."
    ),
)
@click.option(
    "--quota",
    "-q",
    help=(
        "Node resource as 'gpu,cpu,mem' (mem in GiB). The triple chooses "
        "CPU/memory/GPU available per node. Use 'inspire hpc quota "
        "--workspace <name>' to see valid triples. Slurm options below "
        "(--cpus-per-task / --memory-per-cpu / --number-of-tasks) describe "
        "how your program uses each selected node."
    ),
)
@click.option(
    "--project",
    "-p",
    help="Project name. Required unless supplied by --profile.",
)
@click.option("--workspace", help="Workspace name. Required unless supplied by --profile.")
@click.option(
    "--profile",
    "profile_name",
    default=None,
    help="HPC condition profile providing workspace/project/group/quota/image.",
)
@click.option(
    "--image",
    help="Docker image URL or visible image name. Required unless supplied by --profile.",
)
@click.option(
    "--image-type",
    type=click.Choice(["SOURCE_PUBLIC", "SOURCE_PRIVATE", "SOURCE_OFFICIAL"]),
    default="SOURCE_PRIVATE",
    show_default=True,
    help="Image source type.",
)
@click.option(
    "--instance-count",
    type=click.IntRange(1),
    default=1,
    show_default=True,
    help="Number of selected nodes to allocate.",
)
@task_priority_option()
@click.option(
    "--number-of-tasks",
    type=click.IntRange(1),
    default=1,
    show_default=True,
    help="Slurm --ntasks value.",
)
@click.option(
    "--cpus-per-task",
    type=click.IntRange(1),
    default=None,
    help="Slurm --cpus-per-task value. Default: derive from --quota CPU count.",
)
@click.option(
    "--memory-per-cpu",
    type=click.IntRange(1),
    default=None,
    help="Slurm --mem-per-cpu in GiB. Default: derive from --quota memory / CPU.",
)
@click.option(
    "--enable-hyper-threading/--disable-hyper-threading",
    default=False,
    show_default=True,
    help="Enable hyper-threading",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Resolve workspace, project, quota, compute group, image, and Slurm fields, "
        "then print the plan without submitting the HPC job."
    ),
)
@pass_context
def create_hpc(
    ctx: Context,
    name: str,
    entrypoint: str,
    compute_group: Optional[str],
    quota: Optional[str],
    project: Optional[str],
    workspace: Optional[str],
    profile_name: Optional[str],
    image: Optional[str],
    image_type: str,
    instance_count: int,
    priority: Optional[int],
    number_of_tasks: int,
    cpus_per_task: Optional[int],
    memory_per_cpu: Optional[int],
    enable_hyper_threading: bool,
    dry_run: bool,
) -> None:
    """Create a CPU Slurm / HPC batch job.

    Two independent layers:
      * Node-level: --quota gpu,cpu,mem chooses the resources available per
        node; --instance-count chooses how many nodes.
      * Slurm-level: --number-of-tasks / --cpus-per-task / --memory-per-cpu
        describe how your program runs inside those nodes.

    ``-c/--entrypoint`` must be the Slurm script body. Do not include
    ``#SBATCH`` headers; use ``srun`` to launch the program.

    \b
    Examples:
        inspire hpc create -n preprocess --workspace CPU资源空间 --project CI-情境智能 \
          --group HPC-可上网区资源-2 -q 0,20,256 --image hpc-base:v1 \
          -c 'srun bash -lc "python preprocess.py"'
        inspire hpc create -n probe --profile cpu-hpc -c 'srun hostname' --dry-run
    """
    try:
        from inspire.cli.utils.quota_resolver import (
            QuotaMatchError,
            QuotaParseError,
            SCHEDULE_TYPE_HPC,
            build_resource_spec_price,
            parse_quota,
            resolve_quota,
        )

        config, _ = Config.from_files_and_env()

        fields = apply_workload_profile(
            profiles=getattr(config, "profiles", {}),
            kind="hpc",
            profile_name=profile_name,
            values={
                "workspace": workspace,
                "project": project,
                "group": compute_group,
                "image": image,
                "quota": quota,
            },
        )
        workspace = cast(Optional[str], fields["workspace"])
        project = cast(Optional[str], fields["project"])
        compute_group = cast(Optional[str], fields["group"])
        image = cast(Optional[str], fields["image"])
        quota = cast(Optional[str], fields["quota"])

        for field_name, value in (
            ("workspace", workspace),
            ("project", project),
            ("group", compute_group),
            ("quota", quota),
            ("image", image),
        ):
            if not value:
                _handle_error(
                    ctx,
                    "ValidationError",
                    profile_required_message("hpc", field_name),
                    EXIT_CONFIG_ERROR,
                )
                return

        workspace = cast(str, workspace)
        project = cast(str, project)
        compute_group = cast(str, compute_group)
        image = cast(str, image)
        quota = cast(str, quota)

        session = get_web_session()
        resolved_workspace_id = select_workspace_id(
            config,
            explicit_workspace_name=workspace,
            session=session,
        )
        if resolved_workspace_id is None:
            raise ConfigError(profile_required_message("hpc", "workspace"))
        resolved_project_id = _resolve_project_id(
            config,
            project,
            workspace_id=resolved_workspace_id,
            session=session,
            ctx=ctx,
        )
        final_priority = resolve_workspace_task_priority(
            priority,
            session=session,
            workspace_id=resolved_workspace_id,
            project_id=resolved_project_id,
        )
        final_image = image
        if _looks_like_full_slurm_script(entrypoint):
            _handle_error(
                ctx,
                "ValidationError",
                "HPC entrypoint must be the Slurm body, not a full sbatch script.",
                EXIT_CONFIG_ERROR,
                hint="Pass only the lines after the #SBATCH headers and launch the workload with srun.",
            )
            return

        try:
            quota_spec = parse_quota(quota)
        except QuotaParseError as e:
            _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
            return

        try:
            resolved_quota = resolve_quota(
                spec=quota_spec,
                workspace_id=resolved_workspace_id,
                session=session,
                schedule_config_type=SCHEDULE_TYPE_HPC,
                group_override=compute_group,
            )
        except QuotaMatchError as e:
            _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
            return

        quota_id = resolved_quota.quota_id
        resolved_compute_group_id = resolved_quota.logic_compute_group_id
        resource_spec_price = build_resource_spec_price(quota=resolved_quota)

        # Slurm subdivision defaults: assume one task spans the whole node
        # unless the user explicitly carves it up. Total memory per task =
        # node memory; mem-per-cpu = total / cpus-per-task.
        if cpus_per_task is None:
            cpus_per_task = max(1, int(quota_spec.cpu_count))
        if memory_per_cpu is None:
            memory_per_cpu = max(1, int(quota_spec.memory_gib) // max(1, int(cpus_per_task)))

        create_kwargs = build_hpc_create_payload(
            name=name,
            logic_compute_group_id=resolved_compute_group_id,
            project_id=resolved_project_id,
            workspace_id=resolved_workspace_id,
            image=final_image,
            image_type=image_type,
            entrypoint=entrypoint,
            quota_id=quota_id,
            instance_count=instance_count,
            task_priority=final_priority,
            number_of_tasks=number_of_tasks,
            cpus_per_task=cpus_per_task,
            memory_per_cpu=memory_per_cpu,
            enable_hyper_threading=enable_hyper_threading,
            resource_spec_price=resource_spec_price,
        )

        project_text = _project_label(config, resolved_project_id, project)
        workspace_text = workspace_label(session, resolved_workspace_id, workspace)

        if dry_run:
            payload = _hpc_plan_payload(
                name=name,
                create_kwargs=create_kwargs,
                project_label=project_text,
                workspace_label=workspace_text,
                compute_group_name=resolved_quota.compute_group_name,
            )
            if ctx.json_output:
                click.echo(json_formatter.format_json(payload))
                return
            click.echo(human_formatter.format_success(f"Dry run: HPC create plan for {name}"))
            click.echo(f"Project:   {scrub_raw_ids(project_text)}")
            click.echo(f"Workspace: {scrub_raw_ids(workspace_text)}")
            click.echo(f"Resource:  {quota_spec.display()}")
            click.echo(f"Compute:   {scrub_raw_ids(resolved_quota.compute_group_name)}")
            if final_priority is not None:
                click.echo(f"Requested Priority: {final_priority}")
            click.echo(f"Entry:     {scrub_raw_ids(entrypoint)}")
            click.echo("No HPC job was submitted.")
            return

        data = browser_api_module.create_hpc_job(
            payload=create_kwargs,
            session=session,
        )
        created_id = _created_hpc_job_id(data)
        if created_id:
            remember_resource_identity(
                session=session,
                resource_type="hpc",
                resource_id=created_id,
                name=name,
                workspace_id=resolved_workspace_id,
                owner_scope="self",
                status=str(data.get("status") or ""),
            )

        if ctx.json_output:
            click.echo(json_formatter.format_json(_public_output(data)))
            return

        click.echo(human_formatter.format_success(f"HPC job created: {name}"))
        click.echo(
            f"Project:   {scrub_raw_ids(project_text)}"
        )
        click.echo(
            f"Workspace: {scrub_raw_ids(workspace_text)}"
        )
        click.echo(f"Resource:  {quota_spec.display()}")
        click.echo(f"Compute:   {scrub_raw_ids(resolved_quota.compute_group_name)}")
        if final_priority is not None:
            click.echo(f"Requested Priority: {final_priority}")
        click.echo(f"Entry:     {scrub_raw_ids(entrypoint)}")

    except TaskPriorityError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("status")
@click.argument("name")
@click.option("--workspace", required=True, help="Workspace name.")
@pass_context
def status_hpc(ctx: Context, name: str, workspace: str) -> None:
    """Get status/details of an HPC job (pass the job name)."""
    name = _reject_hpc_name_at_boundary(ctx, name)
    try:
        config, _ = Config.from_files_and_env()
        session = get_web_session()
        data = _run_readonly_hpc_operation(
            ctx,
            config=config,
            session=session,
            name=name,
            workspace=workspace,
            limit=10000,
            operation=lambda job_id, live_session: (
                browser_api_module.get_hpc_job_detail(
                    job_id,
                    session=live_session,
                )
            ),
        )

        if ctx.json_output:
            click.echo(json_formatter.format_json(_public_output(data)))
            return

        click.echo("HPC Job Status")
        click.echo(f"Name:   {scrub_raw_ids(data.get('name', 'N/A'))}")
        click.echo(f"Status: {scrub_raw_ids(data.get('status', 'N/A'))}")
        if data.get("priority") is not None:
            click.echo(f"Requested Priority: {data.get('priority')}")
        if data.get("priority_name"):
            click.echo(f"Priority Name: {scrub_raw_ids(data.get('priority_name'))}")
        if data.get("priority_level"):
            click.echo(f"Priority Level: {scrub_raw_ids(data.get('priority_level'))}")
        if data.get("sub_status"):
            click.echo(f"Sub:    {scrub_raw_ids(data.get('sub_status'))}")
        if data.get("created_at"):
            click.echo(f"Created: {scrub_raw_ids(data.get('created_at'))}")
        if data.get("updated_at"):
            click.echo(f"Updated: {scrub_raw_ids(data.get('updated_at'))}")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("instances")
@click.argument("name")
@click.option(
    "--workspace",
    required=True,
    help="Workspace name.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum instances to display (default: 20).",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Show the complete instance list.",
)
@pass_context
def instances_hpc(
    ctx: Context,
    name: str,
    workspace: str,
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List pod/component instances for an HPC job."""
    try:
        output_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    request_limit = (
        output_limit if output_limit is not None else DEFAULT_COLLECTION_LIMIT
    )
    resolution_limit = (
        limit if limit is not None else _DEFAULT_INSTANCE_SCAN_LIMIT
    )

    name = _reject_hpc_name_at_boundary(ctx, name)
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        rows, total = _run_readonly_hpc_operation(
            ctx,
            config=config,
            session=session,
            name=name,
            workspace=workspace,
            limit=resolution_limit,
            operation=lambda job_id, live_session: _fetch_hpc_instances(
                job_id,
                limit=request_limit,
                session=live_session,
                show_all=show_all,
            ),
        )
        page = bound_collection(rows, limit=output_limit, total=total)

        if ctx.json_output:
            payload: dict[str, Any] = {
                "instances": _public_output(page.items),
                "total": page.total,
            }
            if page.truncated:
                payload.update(page.metadata())
                payload["limit"] = output_limit
            click.echo(json_formatter.format_json(payload))
            return

        click.echo(_format_hpc_instances(page.items))
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("stop")
@click.argument("name")
@click.option("--workspace", required=True, help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help="Pick the Nth candidate (1-indexed) when the name is ambiguous.",
)
@pass_context
def stop_hpc(ctx: Context, name: str, workspace: str, pick: Optional[int]) -> None:
    """Stop an HPC job (pass the job name)."""
    name = _reject_hpc_name_at_boundary(ctx, name)
    try:
        config, _ = Config.from_files_and_env()
        session = get_web_session()
        job_id = _resolve_hpc_name_in_workspace(
            ctx,
            config=config,
            session=session,
            name=name,
            workspace=workspace,
            limit=10000,
            pick=pick,
            require_live=True,
        )
        browser_api_module.stop_hpc_job(job_id, session=session)

        if ctx.json_output:
            click.echo(json_formatter.format_json({"name": name, "stopped": True}))
            return
        click.echo(human_formatter.format_success(f"HPC job stopped: {name}"))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("delete")
@click.argument("name")
@click.option("--workspace", required=True, help="Workspace name.")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help="Pick the Nth candidate (1-indexed) when the name is ambiguous.",
)
@pass_context
def delete_hpc(ctx: Context, name: str, workspace: str, yes: bool, pick: Optional[int]) -> None:
    """Permanently delete an HPC job entry (pass the job name).

    \b
    The entry disappears from the platform HPC list. This cannot be
    undone; if the job is still running, `stop` it first.

    \b
    Example:
        inspire hpc delete my-hpc-run --workspace CPU资源空间
    """
    name = _reject_hpc_name_at_boundary(ctx, name)
    if not yes and not ctx.json_output:
        click.confirm(
            f"Permanently delete HPC job '{scrub_raw_ids(name)}'? This cannot be undone.",
            abort=True,
        )

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        job_id = _resolve_hpc_name_in_workspace(
            ctx,
            config=config,
            session=session,
            name=name,
            workspace=workspace,
            limit=10000,
            pick=pick,
            require_live=True,
        )
        browser_api_module.delete_hpc_job(job_id=job_id, session=session)
        workspace_id = select_workspace_id(
            config,
            explicit_workspace_name=workspace,
            session=session,
        )
        if workspace_id:
            forget_resource_identity(
                session=session,
                resource_type="hpc",
                resource_id=job_id,
                name=name,
                workspace_id=workspace_id,
                owner_scope="self",
            )

        if ctx.json_output:
            click.echo(json_formatter.format_json({"name": name, "status": "deleted"}))
            return
        click.echo(human_formatter.format_success(f"HPC job deleted: {name}"))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = [
    "list_hpc",
    "create_hpc",
    "status_hpc",
    "instances_hpc",
    "stop_hpc",
    "delete_hpc",
]
