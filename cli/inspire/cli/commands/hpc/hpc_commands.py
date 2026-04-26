"""HPC job commands for Inspire CLI."""

from __future__ import annotations

from typing import Any, Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import Config, ConfigError
from inspire.cli.utils.id_resolver import resolve_by_name
from inspire.config.workspaces import select_workspace_id
from inspire.platform.openapi import InspireAPIError
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session


def _resolve_hpc_name(ctx: Context, name: str, *, pick: Optional[int] = None) -> str:
    """Resolve an HPC job name to its platform id (``hpc-job-<uuid>``).

    Scope: current user × session workspace, full page.
    """
    def _lister():
        session = get_web_session()
        me = browser_api_module.get_current_user(session=session)
        created_by = str(me.get("id") or me.get("user_id") or "").strip() or None
        jobs, _ = browser_api_module.list_hpc_jobs(
            session=session, created_by=created_by, page_size=10000
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
    )


def _resolve_project_id(config: Config, requested: Optional[str]) -> str:
    """Resolve a project name to the underlying project_id."""
    if requested:
        if requested.startswith("project-"):
            raise ConfigError(
                f"--project takes a project name, not a raw ID ({requested!r}). "
                "See `inspire config context` for available names."
            )
        if requested in config.projects:
            return config.projects[requested]
        for project_id, metadata in config.project_catalog.items():
            if metadata.get("name") == requested:
                return project_id
        available = sorted(
            a
            for a in (
                set(config.projects.keys())
                | {str(m.get("name") or "").strip() for m in config.project_catalog.values()}
            )
            if a
        )
        hint = ", ".join(available) if available else "(run 'inspire config context')"
        raise ConfigError(f"Unknown project: {requested!r}. Available: {hint}")

    if config.job_project_id:
        return config.job_project_id
    raise ConfigError(
        "Missing project. Set --project <name> or configure [context].project in "
        "./.inspire/config.toml."
    )


def _extract_data(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    return data if isinstance(data, dict) else result


def _looks_like_full_slurm_script(entrypoint: str) -> bool:
    stripped = entrypoint.lstrip()
    return stripped.startswith("#!") or "#SBATCH" in entrypoint


def _format_hpc_list_rows(rows: list[dict[str, str]]) -> str:
    """Format HPC job rows into a compact table."""
    if not rows:
        return "No HPC jobs found."

    job_id_width = max(len("Job ID"), *(len(r["job_id"]) for r in rows))
    name_width = max(len("Name"), *(len(r["name"]) for r in rows))
    status_width = max(len("Status"), *(len(r["status"]) for r in rows))
    created_width = max(len("Created"), *(len(r["created_at"]) for r in rows))

    header = (
        f"{'Job ID':<{job_id_width}} {'Name':<{name_width}} "
        f"{'Status':<{status_width}} {'Created':<{created_width}}"
    )
    sep = "-" * len(header)
    lines = ["HPC Jobs", header, sep]
    for row in rows:
        lines.append(
            f"{row['job_id']:<{job_id_width}} "
            f"{row['name']:<{name_width}} "
            f"{row['status']:<{status_width}} "
            f"{row['created_at']:<{created_width}}"
        )
    lines.append(sep)
    lines.append(f"Total: {len(rows)}")
    return "\n".join(lines)


@click.command("list")
@click.option("--workspace", default=None, help="Workspace name (from [workspaces])")
@click.option("--created-by", default=None, help="Filter by creator user ID")
@click.option("--status", "status_filter", default=None, help="Filter by job status")
@click.option("--page-num", type=int, default=1, show_default=True, help="Page number")
@click.option("--page-size", type=int, default=50, show_default=True, help="Page size")
@pass_context
def list_hpc(
    ctx: Context,
    workspace: Optional[str],
    created_by: Optional[str],
    status_filter: Optional[str],
    page_num: int,
    page_size: int,
) -> None:
    """List HPC jobs."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        resolved_workspace_id = None
        if workspace is not None:
            resolved_workspace_id = select_workspace_id(
                config,
                explicit_workspace_name=workspace,
            )

        session = get_web_session()
        jobs, total = browser_api_module.list_hpc_jobs(
            workspace_id=resolved_workspace_id,
            created_by=created_by,
            status=status_filter,
            page_num=page_num,
            page_size=page_size,
            session=session,
        )
        rows = [
            {
                "job_id": job.job_id or "N/A",
                "name": job.name or "N/A",
                "status": job.status or "N/A",
                "created_at": job.created_at or "N/A",
                "entrypoint": job.entrypoint or "",
                "project_name": job.project_name or "",
                "compute_group_name": job.compute_group_name or "",
                "workspace_id": job.workspace_id or "",
            }
            for job in jobs
        ]

        if ctx.json_output:
            click.echo(json_formatter.format_json({"jobs": rows, "total": total}))
            return

        click.echo(_format_hpc_list_rows(rows))

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
    help="Slurm script body (omit #SBATCH headers; use srun for the payload)",
)
@click.option(
    "--compute-group",
    "compute_group",
    required=True,
    help="Compute group name (e.g. 'HPC-可上网区资源-2'; see 'inspire config context').",
)
@click.option(
    "--quota",
    "-q",
    required=True,
    help=(
        "Node-level resource spec as 'gpu,cpu,mem' (mem in GiB). The triple "
        "selects the platform 'compute-spec' (web UI: 计算资源规格), which "
        "determines per-node CPU/memory/GPU totals. Use 'inspire resources "
        "specs --usage hpc' to see available triples. Slurm-level knobs "
        "below (--cpus-per-task / --memory-per-cpu / --number-of-tasks / "
        "--instance-count) are independent — they describe how the slurm "
        "scheduler subdivides the node, not what the node looks like."
    ),
)
@click.option(
    "--project",
    "-p",
    default=None,
    help="Project name (default from [context].project; see 'inspire config context')",
)
@click.option("--workspace", default=None, help="Workspace name (from [workspaces])")
@click.option(
    "--image",
    default=None,
    help="Docker image (default from [job].image)",
)
@click.option("--image-type", default="SOURCE_PRIVATE", show_default=True, help="Image source type")
@click.option("--instance-count", type=int, default=1, show_default=True,
              help="Number of nodes (web UI: 节点数)")
@click.option(
    "--priority",
    type=click.IntRange(1, 10),
    default=None,
    help=(
        "Task priority 1-10 (1-3=LOW preemptible, 4=NORMAL, 5-10=HIGH stable). "
        "Project quota may cap the requested value."
    ),
)
@click.option("--number-of-tasks", type=int, default=1, show_default=True,
              help="Slurm --ntasks (web UI: 子任务数量)")
@click.option("--cpus-per-task", type=int, default=None,
              help="Slurm --cpus-per-task (web UI: 单个任务 CPU 核数). "
                   "Default: derive from --quota cpu count")
@click.option("--memory-per-cpu", type=int, default=None,
              help="Slurm --mem-per-cpu in GiB (web UI: 每 CPU 使用内存 GB). "
                   "Default: derive from --quota mem / --quota cpu")
@click.option(
    "--enable-hyper-threading/--disable-hyper-threading",
    default=False,
    show_default=True,
    help="Enable hyper-threading",
)
@pass_context
def create_hpc(
    ctx: Context,
    name: str,
    entrypoint: str,
    compute_group: str,
    quota: str,
    project: Optional[str],
    workspace: Optional[str],
    image: Optional[str],
    image_type: str,
    instance_count: int,
    priority: Optional[int],
    number_of_tasks: int,
    cpus_per_task: Optional[int],
    memory_per_cpu: Optional[int],
    enable_hyper_threading: bool,
) -> None:
    """Create a Slurm-backed HPC job.

    Two independent layers:
      * Node-level: --quota gpu,cpu,mem picks a 计算资源规格 (which 'spec'
        to allocate per node) + --instance-count says how many such nodes.
      * Slurm-level: --number-of-tasks / --cpus-per-task / --memory-per-cpu
        tell slurm how to subdivide each node.

    ``-c/--entrypoint`` must be the Slurm script body. Do not include
    ``#SBATCH`` headers; use ``srun`` to launch the payload.
    """
    try:
        from inspire.cli.utils.quota_resolver import (
            QuotaMatchError,
            QuotaParseError,
            SCHEDULE_TYPE_HPC,
            parse_quota,
            resolve_quota,
        )

        config, _ = Config.from_files_and_env(require_target_dir=False)
        api = AuthManager.get_api(config)

        resolved_project_id = _resolve_project_id(config, project)
        resolved_workspace_id = select_workspace_id(
            config,
            explicit_workspace_name=workspace,
        )
        if resolved_workspace_id is None:
            raise ConfigError(
                "Missing workspace_id. Set --workspace or configure [job].workspace_id."
            )
        final_priority = priority if priority is not None else config.job_priority
        final_image = image if image is not None else config.job_image
        if not final_image:
            raise ConfigError("Missing image. Set --image or configure [job].image / INSP_IMAGE.")
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
            session = get_web_session()
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

        spec_id = resolved_quota.quota_id
        resolved_compute_group_id = resolved_quota.logic_compute_group_id

        # Slurm subdivision defaults: assume one task spans the whole node
        # unless the user explicitly carves it up. Total memory per task =
        # node memory; mem-per-cpu = total / cpus-per-task.
        if cpus_per_task is None:
            cpus_per_task = max(1, int(quota_spec.cpu_count))
        if memory_per_cpu is None:
            memory_per_cpu = max(1, int(quota_spec.memory_gib) // max(1, int(cpus_per_task)))

        result = api.create_hpc_job(
            name=name,
            logic_compute_group_id=resolved_compute_group_id,
            project_id=resolved_project_id,
            workspace_id=resolved_workspace_id,
            image=final_image,
            image_type=image_type,
            entrypoint=entrypoint,
            spec_id=spec_id,
            instance_count=instance_count,
            task_priority=final_priority,
            number_of_tasks=number_of_tasks,
            cpus_per_task=cpus_per_task,
            memory_per_cpu=memory_per_cpu,
            enable_hyper_threading=enable_hyper_threading,
        )

        data = _extract_data(result)
        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return

        click.echo(human_formatter.format_success(f"HPC job created: {name}"))
        click.echo(f"Project:   {resolved_project_id}")
        click.echo(f"Workspace: {resolved_workspace_id}")
        click.echo(f"Spec:      {spec_id}")
        if final_priority is not None:
            click.echo(f"Requested Priority: {final_priority}")
        click.echo(f"Entry:     {entrypoint}")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except InspireAPIError as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("status")
@click.argument("name")
@pass_context
def status_hpc(ctx: Context, name: str) -> None:
    """Get status/details of an HPC job (pass the job name)."""
    try:
        config, _ = Config.from_files_and_env(require_target_dir=False)
        api = AuthManager.get_api(config)
        job_id = _resolve_hpc_name(ctx, name)
        result = api.get_hpc_job_detail(job_id)
        data = _extract_data(result)

        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return

        click.echo("HPC Job Status")
        click.echo(f"Name:   {data.get('name', 'N/A')}")
        click.echo(f"Status: {data.get('status', 'N/A')}")
        if data.get("priority") is not None:
            click.echo(f"Requested Priority: {data.get('priority')}")
        if data.get("priority_name"):
            click.echo(f"Priority Name: {data.get('priority_name')}")
        if data.get("priority_level"):
            click.echo(f"Priority Level: {data.get('priority_level')}")
        if data.get("sub_status"):
            click.echo(f"Sub:    {data.get('sub_status')}")
        if data.get("created_at"):
            click.echo(f"Created: {data.get('created_at')}")
        if data.get("updated_at"):
            click.echo(f"Updated: {data.get('updated_at')}")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except InspireAPIError as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("stop")
@click.argument("name")
@click.option(
    "--pick",
    type=int,
    default=None,
    help="Pick the Nth candidate (1-indexed) when the name is ambiguous.",
)
@pass_context
def stop_hpc(ctx: Context, name: str, pick: Optional[int]) -> None:
    """Stop an HPC job (pass the job name)."""
    try:
        config, _ = Config.from_files_and_env(require_target_dir=False)
        api = AuthManager.get_api(config)
        job_id = _resolve_hpc_name(ctx, name, pick=pick)
        api.stop_hpc_job(job_id)

        if ctx.json_output:
            click.echo(json_formatter.format_json({"name": name, "stopped": True}))
            return
        click.echo(human_formatter.format_success(f"HPC job stopped: {name}"))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except InspireAPIError as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("delete")
@click.argument("name")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@click.option(
    "--pick",
    type=int,
    default=None,
    help="Pick the Nth candidate (1-indexed) when the name is ambiguous.",
)
@pass_context
def delete_hpc(ctx: Context, name: str, yes: bool, pick: Optional[int]) -> None:
    """Permanently delete an HPC job entry (pass the job name).

    \b
    The entry disappears from the HPC list in the web UI. This cannot be
    undone; if the job is still running, `stop` it first.

    \b
    Example:
        inspire hpc delete my-hpc-run
    """
    if not yes and not ctx.json_output:
        click.confirm(
            f"Permanently delete HPC job '{name}'? This cannot be undone.",
            abort=True,
        )

    try:
        job_id = _resolve_hpc_name(ctx, name, pick=pick)
        session = get_web_session()
        result = browser_api_module.delete_hpc_job(job_id=job_id, session=session)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"name": name, "status": "deleted", "result": result}
                )
            )
            return
        click.echo(human_formatter.format_success(f"HPC job deleted: {name}"))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except (SessionExpiredError, InspireAPIError) as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = ["list_hpc", "create_hpc", "status_hpc", "stop_hpc", "delete_hpc"]
