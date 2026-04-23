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
    """Resolve project name / project_id to the underlying project_id."""
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
        "Missing project_id. Set --project or configure [context].project in "
        "./.inspire/config.toml / INSPIRE_PROJECT_ID."
    )


def _resolve_compute_group_id(config: Config, requested: str) -> str:
    """Resolve a compute-group name (or raw ``lcg-…`` id) to ``logic_compute_group_id``."""
    requested = (requested or "").strip()
    if not requested:
        raise ConfigError("Compute group cannot be empty.")
    if requested.startswith("lcg-"):
        # Accept raw id as an escape hatch; loader ensures such strings are
        # in `config.compute_groups` if the user is looking at a real group.
        return requested
    for group in config.compute_groups or []:
        if group.get("name") == requested:
            group_id = str(group.get("id") or "").strip()
            if group_id:
                return group_id
    available = sorted(
        str(g.get("name") or "").strip()
        for g in (config.compute_groups or [])
        if str(g.get("name") or "").strip()
    )
    hint = ", ".join(available) if available else "(run 'inspire config context')"
    raise ConfigError(
        f"Unknown compute group: {requested!r}. Available: {hint}"
    )


def _extract_memory_gib(price: dict) -> int:
    value = (
        price.get("memory_size_gib")
        or price.get("memory_size")
        or price.get("memory_size_gb")
        or 0
    )
    try:
        return int(value)
    except Exception:
        return 0


def _resolve_hpc_spec_id(
    *,
    session,
    workspace_id: str,
    compute_group_id: str,
    cpus_per_task: int,
    memory_per_cpu: int,
) -> str:
    """Look up the HPC spec_id (predef_quota_id) from (compute_group, cpu, memory).

    The Inspire platform returns one or more HPC specs per compute group
    keyed by ``(cpu_count, memory_size_gib, gpu_count)``. For HPC specifically
    gpu_count is always 0, so ``(compute_group, cpus_per_task,
    cpus_per_task * memory_per_cpu)`` uniquely identifies the quota.

    Raises ``ConfigError`` if no HPC spec in the compute group matches, or
    if more than one matches (should not happen; guards against platform
    changes).
    """
    try:
        prices = browser_api_module.get_resource_prices(
            workspace_id=workspace_id,
            logic_compute_group_id=compute_group_id,
            schedule_config_type="SCHEDULE_CONFIG_TYPE_HPC",
            session=session,
        )
    except Exception as err:
        raise ConfigError(
            f"Could not query HPC specs for compute group {compute_group_id}: {err}. "
            "Run 'inspire resources specs --usage hpc --json' to verify the compute group "
            "exposes HPC quotas."
        ) from err

    target_memory = int(cpus_per_task) * int(memory_per_cpu)
    matches = [
        p
        for p in prices
        if int(p.get("cpu_count") or 0) == int(cpus_per_task)
        and _extract_memory_gib(p) == target_memory
        and int(p.get("gpu_count") or 0) == 0
    ]
    if not matches:
        available = [
            f"cpus={p.get('cpu_count')}/mem={_extract_memory_gib(p)}GiB"
            for p in prices
            if int(p.get("gpu_count") or 0) == 0
        ]
        hint = ", ".join(available) if available else "(this compute group exposes no HPC specs)"
        raise ConfigError(
            f"No HPC spec matches --cpus-per-task={cpus_per_task} "
            f"--memory-per-cpu={memory_per_cpu} (→ {target_memory} GiB total) in this "
            f"compute group. Available HPC specs: {hint}"
        )
    if len(matches) > 1:
        raise ConfigError(
            f"Ambiguous HPC spec: {len(matches)} specs share cpus={cpus_per_task} "
            f"memory={target_memory}GiB. File an issue with the output of "
            "`inspire resources specs --usage hpc --json`."
        )
    spec_id = str(matches[0].get("quota_id") or matches[0].get("spec_id") or "").strip()
    if not spec_id:
        raise ConfigError(
            "Platform returned an HPC spec without a quota_id — cannot submit. "
            "This is a platform bug; report the compute group name."
        )
    return spec_id


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
    "--spec-id",
    default=None,
    help=(
        "Platform predef_quota_id escape hatch. Leave empty — the CLI resolves "
        "it from (--compute-group, --cpus-per-task, --memory-per-cpu)."
    ),
)
@click.option(
    "--project",
    "-p",
    default=None,
    help="Project name/alias/ID (default from [job].project_id)",
)
@click.option("--workspace", default=None, help="Workspace name (from [workspaces])")
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
    compute_group: str,
    spec_id: Optional[str],
    project: Optional[str],
    workspace: Optional[str],
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

        resolved_compute_group_id = _resolve_compute_group_id(config, compute_group)

        if not spec_id:
            session = get_web_session()
            spec_id = _resolve_hpc_spec_id(
                session=session,
                workspace_id=resolved_workspace_id,
                compute_group_id=resolved_compute_group_id,
                cpus_per_task=cpus_per_task,
                memory_per_cpu=memory_per_cpu,
            )

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
