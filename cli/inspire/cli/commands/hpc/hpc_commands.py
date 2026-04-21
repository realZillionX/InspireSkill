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
from inspire.config.workspaces import select_workspace_id
from inspire.platform.openapi import InspireAPIError
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session


def _resolve_project_id(config: Config, requested: Optional[str]) -> str:
    """Resolve project alias/name/id to project_id."""
    if requested:
        if requested.startswith("project-"):
            return requested
        if requested in config.projects:
            return config.projects[requested]
        for project_id, metadata in config.project_catalog.items():
            if metadata.get("name") == requested:
                return project_id
        return requested

    if config.job_project_id:
        return config.job_project_id
    raise ConfigError(
        "Missing project_id. Set --project or configure [job].project_id / INSPIRE_PROJECT_ID."
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
@click.option("--workspace-id", "workspace_id_override", default=None, help="Workspace ID override")
@click.option("--created-by", default=None, help="Filter by creator user ID")
@click.option("--status", "status_filter", default=None, help="Filter by job status")
@click.option("--page-num", type=int, default=1, show_default=True, help="Page number")
@click.option("--page-size", type=int, default=50, show_default=True, help="Page size")
@pass_context
def list_hpc(
    ctx: Context,
    workspace: Optional[str],
    workspace_id_override: Optional[str],
    created_by: Optional[str],
    status_filter: Optional[str],
    page_num: int,
    page_size: int,
) -> None:
    """List HPC jobs."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        resolved_workspace_id = None
        if workspace is not None or workspace_id_override is not None:
            resolved_workspace_id = select_workspace_id(
                config,
                explicit_workspace_name=workspace,
                explicit_workspace_id=workspace_id_override,
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
    "--logic-compute-group-id",
    required=True,
    help="Compute group ID (logic_compute_group_id)",
)
@click.option(
    "--spec-id",
    required=True,
    help="HPC predef_quota_id (use inspire resources specs --usage hpc)",
)
@click.option(
    "--project",
    "-p",
    default=None,
    help="Project name/alias/ID (default from [job].project_id)",
)
@click.option("--workspace", default=None, help="Workspace name (from [workspaces])")
@click.option("--workspace-id", "workspace_id_override", default=None, help="Workspace ID override")
@click.option(
    "--image",
    default=None,
    help="Docker image (default from [job].image)",
)
@click.option("--image-type", default="SOURCE_PRIVATE", show_default=True, help="Image source type")
@click.option("--instance-count", type=int, default=1, show_default=True, help="Instance count")
@click.option(
    "--priority",
    type=click.IntRange(1, 10),
    default=None,
    help="Task priority 1-10 (higher numbers request higher priority; project quota may cap it)",
)
@click.option("--number-of-tasks", type=int, default=1, show_default=True, help="Number of tasks")
@click.option("--cpus-per-task", type=int, required=True, help="CPUs per task")
@click.option("--memory-per-cpu", type=int, required=True, help="Memory per CPU (GiB)")
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
    logic_compute_group_id: str,
    spec_id: str,
    project: Optional[str],
    workspace: Optional[str],
    workspace_id_override: Optional[str],
    image: Optional[str],
    image_type: str,
    instance_count: int,
    priority: Optional[int],
    number_of_tasks: int,
    cpus_per_task: int,
    memory_per_cpu: int,
    enable_hyper_threading: bool,
) -> None:
    """Create a Slurm-backed HPC job.

    ``-c/--entrypoint`` must be the Slurm script body. Do not include ``#SBATCH``
    headers; use ``srun`` to launch the payload.
    """
    try:
        config, _ = Config.from_files_and_env(require_target_dir=False)
        api = AuthManager.get_api(config)

        resolved_project_id = _resolve_project_id(config, project)
        resolved_workspace_id = select_workspace_id(
            config,
            explicit_workspace_name=workspace,
            explicit_workspace_id=workspace_id_override,
        )
        if resolved_workspace_id is None:
            raise ConfigError(
                "Missing workspace_id. Set --workspace-id/--workspace or configure [job].workspace_id."
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

        result = api.create_hpc_job(
            name=name,
            logic_compute_group_id=logic_compute_group_id,
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

        job_id = data.get("job_id", "(not returned)")
        click.echo(human_formatter.format_success(f"HPC job created: {job_id}"))
        click.echo(f"Name:      {name}")
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
@click.argument("job_id")
@pass_context
def status_hpc(ctx: Context, job_id: str) -> None:
    """Get status/details of an HPC job."""
    try:
        config, _ = Config.from_files_and_env(require_target_dir=False)
        api = AuthManager.get_api(config)
        result = api.get_hpc_job_detail(job_id)
        data = _extract_data(result)

        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return

        click.echo("HPC Job Status")
        click.echo(f"Job ID: {data.get('job_id', job_id)}")
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
@click.argument("job_id")
@pass_context
def stop_hpc(ctx: Context, job_id: str) -> None:
    """Stop an HPC job."""
    try:
        config, _ = Config.from_files_and_env(require_target_dir=False)
        api = AuthManager.get_api(config)
        api.stop_hpc_job(job_id)

        if ctx.json_output:
            click.echo(json_formatter.format_json({"job_id": job_id, "stopped": True}))
            return
        click.echo(human_formatter.format_success(f"HPC job stopped: {job_id}"))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except InspireAPIError as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("delete")
@click.argument("job_id")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@pass_context
def delete_hpc(ctx: Context, job_id: str, yes: bool) -> None:
    """Permanently delete an HPC job entry (Browser API).

    \b
    The entry disappears from the HPC list in the web UI. This cannot be
    undone; if the job is still running, `stop` it first.

    \b
    Example:
        inspire hpc delete hpc-3eabc123-...
    """
    if not yes and not ctx.json_output:
        click.confirm(
            f"Permanently delete HPC job '{job_id}'? This cannot be undone.",
            abort=True,
        )

    try:
        session = get_web_session()
        result = browser_api_module.delete_hpc_job(job_id=job_id, session=session)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"job_id": job_id, "status": "deleted", "result": result}
                )
            )
            return
        click.echo(human_formatter.format_success(f"HPC job deleted: {job_id}"))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except (SessionExpiredError, InspireAPIError) as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = ["list_hpc", "create_hpc", "status_hpc", "stop_hpc", "delete_hpc"]
