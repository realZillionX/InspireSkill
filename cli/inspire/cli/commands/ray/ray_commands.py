"""Ray (弹性计算) job commands for Inspire CLI."""

from __future__ import annotations
from inspire.services.ray.ray_submission import created_ray_job_id as _created_ray_job_id

from inspire.services.ray.ray_events import (
    fetch_recent_ray_events as _fetch_recent_ray_events,
)

from inspire.services.ray.ray_status import matches_status


from inspire.services.ray.ray_instances import (
    public_ray_instance_text as _public_ray_instance_text,
    ray_instance_rank as _ray_instance_rank,
    RayInstanceSelectionError as RayInstanceSelectionError,
    RayInstanceView as RayInstanceView,
    ray_instance_views as ray_instance_views,
    select_ray_instance_views as select_ray_instance_views,
    fetch_ray_instances as _fetch_ray_instances,
)

import logging
import sys
import time
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
from inspire.cli.formatters import human_formatter
from inspire.services.utils import json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.services.utils.collections import (
    DEFAULT_COLLECTION_LIMIT,
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import (
    exit_with_error as _handle_error,
    require_confirmation,
)
from inspire.cli.utils.events import (
    DEFAULT_EVENT_TAIL,
    run_events_command,
)
from inspire.cli.utils.id_resolver import (
    NAME_PICK_HELP,
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
from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.task_priority import (
    TaskPriorityError,
    resolve_workspace_task_priority,
    task_priority_option,
)
from inspire.config import Config, ConfigError
from inspire.config.workspaces import (
    resolve_workspace_query_scope,
    select_workspace_id,
    workspace_label,
    workspace_name_map,
)
from inspire.platform.web.pty_socket import JobShellError
from inspire.cli.utils.job_shell import open_job_shell
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session

from inspire.services.ray.ray_output import (
    format_ray_status,
    public_ray_list_item,
    public_ray_status,
)

_DEFAULT_INSTANCE_SCAN_LIMIT = 500
logger = logging.getLogger(__name__)

IMAGE_TYPE_CHOICES = ["SOURCE_PUBLIC", "SOURCE_PRIVATE", "SOURCE_OFFICIAL"]


def _current_user_id(session) -> str:  # noqa: ANN001
    me = browser_api_module.get_current_user(session=session)
    user_id = str(me.get("id") or me.get("user_id") or "").strip()
    if not user_id:
        raise ValueError("Cannot determine the current user from the live web session.")
    return user_id


def _resolve_ray_name_in_workspace(
    ctx: Context,
    *,
    session,
    name: str,
    workspace: str,
    limit: int,
    pick: Optional[int] = None,
    require_live: bool = False,
) -> str:
    workspace_id = select_workspace_id(
        explicit_workspace_name=workspace,
        session=session,
    )
    if workspace_id is None:
        raise ConfigError("--workspace is required.")
    user_id = _current_user_id(session)

    def _lister():
        jobs, _ = browser_api_module.list_ray_jobs(
            workspace_id=workspace_id,
            user_ids=[user_id],
            page_num=1,
            page_size=limit,
            session=session,
        )
        return [
            {
                "name": j.name,
                "id": j.ray_job_id,
                "status": j.status,
                "workspace_id": j.workspace_id,
                "created_at": j.created_at,
            }
            for j in jobs
        ]

    return resolve_by_name(
        ctx,
        name=name,
        resource_type="ray",
        list_candidates=_lister,
        pick_index=pick,
        session=session,
        workspace_id=workspace_id,
        owner_scope="self",
        require_live=require_live,
        list_command=f"inspire ray list --workspace {workspace}",
    )


def _reject_ray_name_at_boundary(ctx: Context, name: str) -> str:
    return reject_id_at_boundary(
        ctx,
        name,
        resource_type="ray",
        list_command="inspire ray list --workspace <workspace>",
    )


def _run_readonly_ray_operation(
    ctx: Context,
    *,
    session,
    name: str,
    workspace: str,
    limit: int,
    pick: Optional[int] = None,
    operation,
):
    """Run a read-only Ray operation and recover one stale cache hit."""
    def _resolve(require_live: bool) -> str:
        return _resolve_ray_name_in_workspace(
            ctx,
            session=session,
            name=name,
            workspace=workspace,
            limit=limit,
            pick=pick,
            require_live=require_live,
        )

    def _invalidate(job_id: str) -> None:
        workspace_id = select_workspace_id(
            explicit_workspace_name=workspace,
            session=session,
        )
        if workspace_id is None:
            raise ConfigError("--workspace is required.")
        forget_resource_identity(
            session=session,
            resource_type="ray",
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


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _format_ray_list_rows(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "No Ray jobs found."

    show_workspace = any(row.get("workspace") for row in rows)
    headers = ["Name", "Status"]
    if show_workspace:
        headers.append("Workspace")
    headers.extend(("Created", "Created By"))
    table_rows = [
        (
            row["name"],
            row["status"],
            *([row.get("workspace", "")] if show_workspace else []),
            row["created_at"],
            row["created_by_name"],
        )
        for row in rows
    ]
    widths = [
        column_width(header, [row[index] for row in table_rows], max_width=64)
        for index, header in enumerate(headers)
    ]
    rendered = render_table(headers, table_rows, widths, line_char="─")
    return "\n".join(rendered)


def _ray_instance_resource(inst: dict[str, Any]) -> str:
    direct = _public_ray_instance_text(inst, "resource")
    if direct:
        return direct

    spec = inst
    for key in ("resource_spec", "resource_spec_price", "quota"):
        candidate = inst.get(key)
        if isinstance(candidate, dict):
            spec = candidate
            break

    values = (
        ("CPU", _public_ray_instance_text(spec, "cpu_count", "cpu")),
        (
            "GiB",
            _public_ray_instance_text(
                spec,
                "memory_size_gib",
                "memory_gib",
                "memory_size",
                "memory",
            ),
        ),
        ("GPU", _public_ray_instance_text(spec, "gpu_count", "gpu")),
    )
    return ", ".join(f"{value} {unit}" for unit, value in values if value)


def _public_ray_instances(
    instances: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for position, inst in enumerate(instances):
        item: dict[str, Any] = {}
        name = _public_ray_instance_text(
            inst,
            "name",
            "instance_name",
            "display_name",
        )
        if name and not looks_like_platform_id(name):
            item["name"] = name

        for key, candidates in (
            ("status", ("status", "instance_status", "phase", "state")),
            ("role", ("role", "worker_group_name", "component")),
            ("type", ("type", "instance_type")),
            ("node", ("node_name", "node", "host_name")),
        ):
            value = _public_ray_instance_text(inst, *candidates)
            if value:
                item[key] = value

        resource = _ray_instance_resource(inst)
        if resource:
            item["resource"] = resource
        item["rank"] = _ray_instance_rank(inst, position)
        projected.append(item)
    return projected


def _format_ray_instances(instances: list[dict[str, Any]]) -> str:
    if not instances:
        return "No Ray instances found."

    columns = [("name", "Name"), ("status", "Status")]
    columns.extend(
        (key, label)
        for key, label in (
            ("role", "Role"),
            ("type", "Type"),
            ("node", "Node"),
            ("resource", "Resource"),
            ("rank", "Rank"),
        )
        if any(item.get(key) not in (None, "") for item in instances)
    )
    table_rows = [
        tuple(
            (
                item.get("name")
                or f"rank={item.get('rank')}"
                if key == "name"
                else item.get(key, "-")
            )
            for key, _ in columns
        )
        for item in instances
    ]
    widths = [
        column_width(label, [row[index] for row in table_rows], max_width=48)
        for index, (_, label) in enumerate(columns)
    ]
    rendered = render_table(
        tuple(label for _, label in columns),
        table_rows,
        widths,
    )
    return "\n".join(rendered)


def _ray_matches_list_filters(
    job: Any,
    *,
    status: Optional[str],
    keyword: Optional[str],
    workspace_name: str = "",
) -> bool:
    """Apply the public Ray list filters to readable job fields."""
    if not matches_status(str(getattr(job, "status", "") or ""), status):
        return False
    if not keyword:
        return True

    needle = keyword.strip().casefold()
    if not needle:
        return True
    fields = (
        getattr(job, "name", ""),
        getattr(job, "status", ""),
        getattr(job, "project_name", ""),
        getattr(job, "created_by_name", ""),
        workspace_name,
    )
    return any(needle in str(field or "").casefold() for field in fields)


@click.command("list")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME|all",
    help="Workspace name or 'all'.",
)
@click.option(
    "--status",
    "-s",
    "status_filter",
    default=None,
    metavar="STATUS",
    help="Case-insensitive status filter.",
)
@click.option(
    "--keyword",
    default=None,
    metavar="KEYWORD",
    help="Case-insensitive keyword filter for job name and readable fields.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum Ray jobs to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every Ray job.")
@pass_context
def list_ray(
    ctx: Context,
    workspace: Optional[str],
    status_filter: Optional[str],
    keyword: Optional[str],
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List Ray (弹性计算) jobs in one or every visible workspace.

    Status in JSON and human output is scrubbed, then uppercased; blank is UNKNOWN.
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
        workspace_ids, all_workspaces = resolve_workspace_query_scope(
            workspace=workspace,
            session=session,
        )

        user_ids: Optional[list[str]] = [_current_user_id(session)]
        workspace_names = workspace_name_map(session)
        local_filter = bool(
            (status_filter and status_filter.strip())
            or (keyword and keyword.strip())
        )

        jobs: list[Any] = []
        total = 0
        for workspace_id in workspace_ids:
            workspace_jobs, workspace_total = browser_api_module.list_ray_jobs(
                workspace_id=workspace_id,
                user_ids=user_ids,
                page_num=1,
                page_size=request_limit,
                session=session,
            )
            if (show_all or local_filter) and workspace_total > len(workspace_jobs):
                workspace_jobs, expanded_total = browser_api_module.list_ray_jobs(
                    workspace_id=workspace_id,
                    user_ids=user_ids,
                    page_num=1,
                    page_size=max(workspace_total, len(workspace_jobs), 1),
                    session=session,
                )
                workspace_total = max(
                    workspace_total,
                    expanded_total,
                    len(workspace_jobs),
                )
            jobs.extend(workspace_jobs)
            total += max(workspace_total, len(workspace_jobs))
        if all_workspaces:
            jobs.sort(key=lambda item: str(item.created_at or ""), reverse=True)

        filtered_jobs = [
            job
            for job in jobs
            if _ray_matches_list_filters(
                job,
                status=status_filter,
                keyword=keyword,
                workspace_name=workspace_names.get(job.workspace_id, ""),
            )
        ]
        if local_filter:
            total = len(filtered_jobs)

        page = bound_collection(filtered_jobs, limit=effective_limit, total=total)
        public_items = [
            public_ray_list_item(
                job,
                workspace=(
                    workspace_names.get(job.workspace_id)
                    or (
                        str(workspace or "")
                        if not all_workspaces
                        else "(workspace name unavailable)"
                    )
                ),
            )
            for job in page.items
        ]
        rows: list[dict[str, str]] = []
        for job in page.items:
            row = {
                "name": scrub_raw_ids(job.name or "N/A"),
                "status": public_ray_list_item(job)["status"],
                "created_at": scrub_raw_ids(job.created_at or "N/A"),
                "created_by_name": scrub_raw_ids(job.created_by_name or "N/A"),
                "project_name": scrub_raw_ids(job.project_name or ""),
            }
            if all_workspaces:
                row["workspace"] = scrub_raw_ids(
                    workspace_names.get(job.workspace_id)
                    or "(workspace name unavailable)"
                )
            rows.append(row)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "items": public_items,
                        **page.metadata(),
                    }
                ),
            )
            return

        click.echo(_format_ray_list_rows(rows))
        notice = truncation_notice(page, full_option="--all")
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


# ---------------------------------------------------------------------------
# status (detail)
# ---------------------------------------------------------------------------


@click.command("status")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def status_ray(ctx: Context, name: str, workspace: str, pick: Optional[int]) -> None:
    """Show details for a Ray (弹性计算) job.

    Status in JSON and human output is scrubbed, then uppercased; blank is UNKNOWN.

    NAME is the Ray job name shown in `inspire ray list`. Plain output shows
    the compact public status view; ``--json`` returns the same stable fields
    in machine-readable form.
    """
    name = _reject_ray_name_at_boundary(ctx, name)
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        data = _run_readonly_ray_operation(
            ctx,
            session=session,
            name=name,
            workspace=workspace,
            limit=10000,
            pick=pick,
            operation=lambda ray_job_id, live_session: (
                browser_api_module.get_ray_job_detail(
                    ray_job_id,
                    session=live_session,
                )
            ),
        )

        detail = public_ray_status(data, fallback_name=name)
        if ctx.json_output:
            click.echo(json_formatter.format_json(detail))
            return

        click.echo(format_ray_status(detail))

    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


# A write reports success only once the state has actually moved, never on the
# strength of the response envelope. Controlled live verification over repeated
# stop/start cycles found `ray.StartJob` honest — its echoed `ray_job` matches a
# fresh `GetJob` field for field, and the job leaves STOPPED at once — so this
# is a guard rather than a workaround, and it costs a single read on the path
# that succeeds. The attempts below are what remains for a platform that
# accepts the request and lags, or stops acting on it.
_RAY_START_CONFIRM_ATTEMPTS = 6
_RAY_START_CONFIRM_INTERVAL_SECONDS = 2.5


def _confirm_ray_left_stopped(
    ray_job_id: str,
    *,
    session,  # noqa: ANN001
) -> str:
    """Poll briefly for the job to leave STOPPED; return the observed status."""
    status = ""
    for attempt in range(_RAY_START_CONFIRM_ATTEMPTS):
        if attempt:
            time.sleep(_RAY_START_CONFIRM_INTERVAL_SECONDS)
        detail = browser_api_module.get_ray_job_detail(ray_job_id, session=session)
        status = str(detail.get("status") or "").strip()
        if status and status.upper() != "STOPPED":
            return status
    return status


@click.command("start")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def start_ray(ctx: Context, name: str, workspace: str, pick: Optional[int]) -> None:
    """Restart a stopped Ray (弹性计算) job.

    The platform keeps the head and worker-group spec on the record, so a job
    stopped with `inspire ray stop` comes back with the same cluster shape and
    driver command; nothing has to be re-specified.

    Only a stopped job can be started; the command reports what the job's
    status actually became rather than what the platform answered. Follow the
    startup with `inspire ray events <name> --workspace <workspace>`.
    """
    name = _reject_ray_name_at_boundary(ctx, name)
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        ray_job_id = _resolve_ray_name_in_workspace(
            ctx,
            session=session,
            name=name,
            workspace=workspace,
            limit=10000,
            pick=pick,
            require_live=True,
        )
        browser_api_module.start_ray_job(ray_job_id, session=session)
        status = _confirm_ray_left_stopped(ray_job_id, session=session)

        if not status or status.upper() == "STOPPED":
            _handle_error(
                ctx,
                "APIError",
                f"Ray job {scrub_raw_ids(name)!r} is still stopped; "
                "the platform accepted the start request without acting on it.",
                EXIT_API_ERROR,
                hint=(
                    "A restart normally leaves STOPPED at once. Read "
                    f"`inspire ray events {scrub_raw_ids(name)} --workspace "
                    f"{scrub_raw_ids(workspace)}` for why the cluster did not "
                    "come back."
                ),
            )
            return

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"name": name, "status": "started", "job_status": status}
                ),
            )
            return
        click.echo(human_formatter.format_mutation_success("Ray", "started", name))

    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("stop")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def stop_ray(ctx: Context, name: str, workspace: str, pick: Optional[int]) -> None:
    """Stop a running Ray (弹性计算) job.

    The record survives; `inspire ray start <name>` brings the same cluster
    back. Use `inspire ray delete` to remove the record entirely.
    """
    name = _reject_ray_name_at_boundary(ctx, name)
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        ray_job_id = _resolve_ray_name_in_workspace(
            ctx,
            session=session,
            name=name,
            workspace=workspace,
            limit=10000,
            pick=pick,
            require_live=True,
        )
        browser_api_module.stop_ray_job(ray_job_id, session=session)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"name": name, "status": "stopped"}
                ),
            )
            return
        click.echo(human_formatter.format_mutation_success("Ray", "stopped", name))

    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


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


def _project_label(config: Config, requested: Optional[str]) -> str:
    if requested:
        return project_display_name(config, requested)
    return "(project name unavailable)"


def _resolve_image_id(raw: str, *, session, ctx: Context, workspace_id: str) -> str:
    from inspire.services.ray.ray_submission import resolve_image_id
    return resolve_image_id(raw, session=session, workspace_id=workspace_id, debug=ctx.debug, log=logger)


def _parse_worker_spec(raw: str) -> dict[str, Any]:
    from inspire.services.ray.ray_submission import parse_worker_spec
    try:
        return parse_worker_spec(raw)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc


@click.command("create")
@click.option("--name", "-n", required=True, metavar="NAME", help="Ray job name")
@click.option(
    "--command",
    "-c",
    required=True,
    help="Driver startup command. The Ray job stays alive while this command keeps running.",
)
@click.option(
    "--workspace",
    required=True,
    metavar="NAME",
    help="Workspace name.",
)
@click.option(
    "--project",
    "-p",
    required=True,
    metavar="NAME",
    help="Project name.",
)
@click.option(
    "--group",
    required=True,
    default=None,
    metavar="NAME",
    help=(
        "Full compute group name copied from the same quota row as --quota."
    ),
)
@click.option(
    "--quota",
    "-q",
    required=True,
    default=None,
    metavar="SPEC",
    help=(
        "Head node resource quota as 'gpu,cpu,mem' (mem in GiB). "
        "CLI resolves the triple against 'inspire ray quota --workspace <name>'."
    ),
)
@click.option(
    "--image",
    "-i",
    required=True,
    default=None,
    metavar="NAME|URL",
    help="Head node image name or Docker URL.",
)
@click.option("--description", default="", help="Free-form description")
@task_priority_option()
@click.option(
    "--image-type",
    type=click.Choice(IMAGE_TYPE_CHOICES),
    default="SOURCE_PUBLIC",
    show_default=True,
    help="Head node image source type.",
)
@click.option(
    "--shm-size",
    type=click.IntRange(1),
    default=None,
    help="Head shared memory in GiB (optional)",
)
@click.option(
    "--worker",
    "workers",
    multiple=True,
    metavar="SPEC",
    help=(
        "Worker group spec (repeatable). Format (note ';' separator): "
        "'name=<grp>;image=<url-or-name>;group=<full-group-name>;quota=<gpu,cpu,mem>;"
        "min=<n>;max=<n>[;image-type=SOURCE_PUBLIC][;shm-size=<gib>]'"
    ),
)
@click.option(
    "--public-path-readonly/--no-public-path-readonly",
    default=None,
    help=(
        "Mount the project's public path read-only inside every Ray container "
        "(平台 高级设置·项目Public只读挂载). Omit to leave the platform default."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Resolve names, images, quotas, and worker groups, then print the plan without submitting.",
)
@pass_context
def create_ray(
    ctx: Context,
    name: Optional[str],
    command: Optional[str],
    description: str,
    project: Optional[str],
    workspace: Optional[str],
    priority: Optional[int],
    image: Optional[str],
    image_type: str,
    group: Optional[str],
    quota: Optional[str],
    shm_size: Optional[int],
    workers: tuple[str, ...],
    public_path_readonly: Optional[bool],
    dry_run: bool,
) -> None:
    """Create a Ray (弹性计算) job with one head and one or more worker groups.

    Resource sizing uses the same ``--quota gpu,cpu,mem`` triple as
    notebook / job. Choose valid triples with
    ``inspire ray quota --workspace <name>``. The driver command should exit
    when the Ray work is done; otherwise the cluster continues to occupy
    quota until stopped.

    \b
    Example:
        inspire ray create \\
          -n av-pipeline \\
          -c 'python driver.py --mode run_and_exit' \\
          --workspace CPU资源空间 \\
          --project <project> \\
          --image <image> \\
          --group HPC-可上网区资源-2 --quota 0,4,16 \\
          --worker 'name=decode;image=<image>;group=HPC-可上网区资源-2;quota=0,20,80;min=1;max=8;shm-size=32'

    """
    try:
        config, _ = Config.from_files_and_env()
        session = get_web_session()

        body = _assemble_create_body(
            ctx,
            config=config,
            session=session,
            name=name,
            command=command,
            description=description,
            project=project,
            workspace=workspace,
            priority=priority,
            image=image,
            image_type=image_type,
            group=group,
            quota=quota,
            shm_size=shm_size,
            workers=workers,
            public_path_readonly=public_path_readonly,
        )

        if dry_run:
            from inspire.services.ray.ray_submission import ray_plan_payload
            from inspire.cli.utils.quota_resolver import parse_quota
            head_spec = parse_quota(cast(str, quota))
            plan = ray_plan_payload(
                body=body, workspace=workspace_label(session, str(body.get("workspace_id") or ""), workspace),
                project=_project_label(config, project), group=cast(str, group),
                quota=cast(str, quota), image=cast(str, image), workers=workers,
                description=description, shm_size=shm_size, public_path_readonly=public_path_readonly,
            )
            worker_plans = plan["workers"]
            if ctx.json_output:
                click.echo(json_formatter.format_json(plan))
                return
            click.echo(f"Create plan: {scrub_raw_ids(plan['name'])}")
            click.echo(f"Project: {scrub_raw_ids(plan['project'])}")
            click.echo(f"Workspace: {scrub_raw_ids(plan['workspace'])}")
            click.echo(f"Compute: {scrub_raw_ids(plan['compute_group'])}")
            click.echo(f"Resource: {head_spec.display()}")
            if body.get("task_priority") is not None:
                click.echo(f"Priority: {body['task_priority']}")
            if shm_size is not None:
                click.echo(f"Shared memory: {shm_size} GiB")
            click.echo(f"Image: {scrub_raw_ids(image)}")
            if public_path_readonly is not None:
                click.echo(
                    "Public path: read-only"
                    if public_path_readonly
                    else "Public path: writable"
                )
            click.echo(f"Command: {scrub_raw_ids(body.get('entrypoint'))}")
            click.echo(f"Workers: {len(worker_plans)}")
            for worker in worker_plans:
                worker_resource = worker["resource"]
                assert isinstance(worker_resource, dict)
                click.echo(
                    "  "
                    f"{scrub_raw_ids(worker['name'])}: "
                    f"{worker_resource['gpu']},{worker_resource['cpu']},"
                    f"{worker_resource['memory_gib']} "
                    f"on {scrub_raw_ids(worker['compute_group'])} "
                    f"({worker['min_replicas']}-{worker['max_replicas']} replicas)"
                )
            return

        data = browser_api_module.create_ray_job(body, session=session)
        created_id = _created_ray_job_id(data)
        if created_id:
            remember_resource_identity(
                session=session,
                resource_type="ray",
                resource_id=created_id,
                name=str(body.get("name") or ""),
                workspace_id=str(body.get("workspace_id") or ""),
                owner_scope="self",
                status=str(data.get("status") or ""),
            )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "name": str(body.get("name") or name or ""),
                        "status": "created",
                    }
                )
            )
            return

        click.echo(
            human_formatter.format_mutation_success(
                "Ray",
                "created",
                body.get("name") or name or "",
            )
        )

    except TaskPriorityError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except click.UsageError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def _assemble_create_body(
    ctx: Context,
    *,
    config: Config,
    session,
    name: Optional[str],
    command: Optional[str],
    description: str,
    project: Optional[str],
    workspace: Optional[str],
    priority: Optional[int],
    image: Optional[str],
    image_type: str,
    group: Optional[str],
    quota: Optional[str],
    shm_size: Optional[int],
    workers: tuple[str, ...],
    public_path_readonly: Optional[bool] = None,
) -> dict[str, Any]:
    from inspire.services.ray.ray_submission import assemble_create_body, validate_create_inputs
    from inspire.cli.utils.quota_resolver import parse_quota, resolve_quota, SCHEDULE_TYPE_RAY

    try:
        validate_create_inputs(
            name=name, command=command, image=image, group=group, quota=quota,
            workspace=workspace, project=project, image_type=image_type, workers=workers,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    workspace_id = select_workspace_id(explicit_workspace_name=workspace, session=session)
    if workspace_id is None:
        raise ConfigError("--workspace is required.")
    project_id = _resolve_project_id(
        config, project, workspace_id=workspace_id, session=session, ctx=ctx
    )
    try:
        return assemble_create_body(
            workspace_id=workspace_id, project_id=project_id,
            resolve_quota=lambda triple, group_name: resolve_quota(
                spec=parse_quota(triple), workspace_id=workspace_id, session=session,
                schedule_config_type=SCHEDULE_TYPE_RAY, group_override=group_name,
            ),
            resolve_image=lambda raw: _resolve_image_id(
                raw, session=session, ctx=ctx, workspace_id=workspace_id
            ),
            resolve_priority=lambda requested: resolve_workspace_task_priority(
                requested, session=session, workspace_id=workspace_id, project_id=project_id
            ),
            name=name, command=command, description=description, project=project,
            workspace=workspace, priority=priority, image=image, image_type=image_type,
            group=group, quota=quota, shm_size=shm_size, workers=workers,
            public_path_readonly=public_path_readonly,
        )
    except (TaskPriorityError, ConfigError):
        raise
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc


_RAY_EVENT_NAME_SCAN_LIMIT = 500
_RAY_EVENT_PAGE_SIZE = 200
_RAY_EVENT_MAX_PAGES = 5


@click.command("events")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option(
    "--type",
    "type_filter",
    type=click.Choice(["Normal", "Warning"], case_sensitive=False),
    default=None,
    help="Filter by event type.",
)
@click.option(
    "--reason",
    default=None,
    metavar="REASON",
    help="Filter by event reason (e.g. FailedScheduling, CreatedRayCluster).",
)
@click.option(
    "--instance",
    "instance_selectors",
    multiple=True,
    metavar="ROLE",
    help=(
        "Narrow to one instance, named by the Role / Type (and Rank, when "
        "several share one) column of `inspire ray instances` — for example "
        "head or a worker-group name. Repeat for several. Default: cluster "
        "events plus every pod."
    ),
)
@click.option(
    "--workload-level",
    "workload_level",
    is_flag=True,
    help=(
        "Only the controller's own events about the cluster as a whole. "
        "Cannot be combined with --instance."
    ),
)
@click.option(
    "--tail",
    type=click.IntRange(1),
    default=DEFAULT_EVENT_TAIL,
    show_default=True,
    help="Maximum recent events to display.",
)
@click.option(
    "--follow",
    "-f",
    is_flag=True,
    help=(
        "Follow the event timeline and print new events. Runs until interrupted; it never exits on its own, "
        "not even once the job reaches a terminal state."
    ),
)
@click.option(
    "--interval",
    type=click.IntRange(1),
    default=5,
    show_default=True,
    help="Polling interval in seconds for --follow.",
)
@pass_context
def events_ray(
    ctx: Context,
    name: str,
    workspace: str,
    pick: Optional[int],
    reason: Optional[str],
    type_filter: Optional[str],
    instance_selectors: tuple[str, ...],
    workload_level: bool,
    tail: int,
    follow: bool,
    interval: int,
) -> None:
    """Show events for a Ray (弹性计算) job.

    Critical for diagnosing stuck PENDING jobs — the `FailedScheduling` events
    spell out exactly why the scheduler can't place a pod (insufficient CPU /
    GPU, node affinity mismatch, taint, etc.). Cluster events and every pod's
    events arrive in one timeline with an `Instance` column; `--instance`
    narrows to one role and `--workload-level` keeps only the controller's
    half.

    \b
    Examples:
        inspire ray events pipeline --workspace CPU资源空间
        inspire ray events pipeline --workspace CPU资源空间 --reason FailedScheduling
        inspire ray events pipeline --workspace CPU资源空间 --type Warning --tail 10
        inspire ray events pipeline --workspace CPU资源空间 --instance head
        inspire ray events pipeline --workspace CPU资源空间 --workload-level
        inspire ray events pipeline --workspace CPU资源空间 --follow
        inspire --json ray events pipeline --workspace CPU资源空间
    """
    name = _reject_ray_name_at_boundary(ctx, name)
    if workload_level and instance_selectors:
        _handle_error(
            ctx,
            "InvalidUsage",
            "--workload-level and --instance cannot be used together.",
            EXIT_VALIDATION_ERROR,
        )
        return
    try:
        session = get_web_session()
        config, _ = Config.from_files_and_env(require_credentials=False)

        def _fetch_events() -> list[dict]:
            # An unknown `--instance` is a usage error, and the shared runner
            # would otherwise repackage it as "could not fetch events".
            try:
                return _run_readonly_ray_operation(
                    ctx,
                    session=session,
                    name=name,
                    workspace=workspace,
                    limit=_RAY_EVENT_NAME_SCAN_LIMIT,
                    pick=pick,
                    operation=lambda ray_job_id, live_session: (
                        _fetch_recent_ray_events(
                            ray_job_id,
                            session=live_session,
                            selectors=instance_selectors,
                            workload_level=workload_level,
                        )
                    ),
                )
            except RayInstanceSelectionError as e:
                _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
                return []

        run_events_command(
            ctx,
            fetch=_fetch_events,
            type_filter=type_filter,
            reason_filter=reason,
            tail=tail,
            follow=follow,
            interval=interval,
        )

    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


# ---------------------------------------------------------------------------
# instances
# ---------------------------------------------------------------------------


@click.command("instances")
@click.argument("name", metavar="NAME")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME",
    help="Workspace name.",
)
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
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
def instances_ray(
    ctx: Context,
    name: str,
    workspace: str,
    pick: Optional[int],
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List pod-level instances (head + workers) for a Ray job.

    NAME is resolved within the explicit workspace and current live user. Shows
    each pod's status; check `inspire ray events <name> --workspace
    <workspace>` for scheduler reasons when pods remain pending.
    """
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

    name = _reject_ray_name_at_boundary(ctx, name)
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        instances, total = _run_readonly_ray_operation(
            ctx,
            session=session,
            name=name,
            workspace=workspace,
            limit=resolution_limit,
            pick=pick,
            operation=lambda ray_job_id, live_session: _fetch_ray_instances(
                ray_job_id,
                limit=request_limit,
                session=live_session,
                show_all=show_all,
            ),
        )
        page = bound_collection(instances, limit=output_limit, total=total)
        public_items = _public_ray_instances(page.items)

        if ctx.json_output:
            payload: dict[str, Any] = {
                "name": scrub_raw_ids(name),
                "items": public_items,
                **page.metadata(),
            }
            click.echo(json_formatter.format_json(payload))
            return

        click.echo(_format_ray_instances(public_items))
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@click.command("delete")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
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
    help=NAME_PICK_HELP,
)
@pass_context
def delete_ray(ctx: Context, name: str, workspace: str, yes: bool, pick: Optional[int]) -> None:
    """Permanently delete a Ray (弹性计算) job record.

    The entry disappears from the platform Ray list. This cannot be undone; if
    the job is still running, `stop` it first so the scheduler releases
    reserved capacity cleanly.
    """
    name = _reject_ray_name_at_boundary(ctx, name)
    require_confirmation(
        ctx,
        yes=yes,
        prompt=(
            f"Permanently delete Ray job '{scrub_raw_ids(name)}'? "
            "This cannot be undone."
        ),
        message="Ray job deletion requires confirmation.",
    )

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        ray_job_id = _resolve_ray_name_in_workspace(
            ctx,
            session=session,
            name=name,
            workspace=workspace,
            limit=10000,
            pick=pick,
            require_live=True,
        )
        browser_api_module.delete_ray_job(ray_job_id, session=session)
        workspace_id = select_workspace_id(
            explicit_workspace_name=workspace,
            session=session,
        )
        if workspace_id:
            forget_resource_identity(
                session=session,
                resource_type="ray",
                resource_id=ray_job_id,
                name=name,
                workspace_id=workspace_id,
                owner_scope="self",
            )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json({"name": name, "status": "deleted"}),
            )
            return
        click.echo(human_formatter.format_mutation_success("Ray", "deleted", name))

    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("shell")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
@click.option(
    "--instance",
    "instance",
    default=None,
    metavar="ROLE",
    help="Open this Role / Rank, as printed by `inspire ray instances`.",
)
@pass_context
def shell_ray(
    ctx: Context,
    name: str,
    workspace: str,
    pick: Optional[int],
    instance: Optional[str],
) -> None:
    """Open an interactive shell inside a running Ray instance.

    Needs a terminal: this attaches your stdin to a remote PTY. Leave with
    `exit`, or press Ctrl+] to drop the session without ending the shell.

    Defaults to the head, which runs the driver and is where `ray status` and
    the cluster's own logs live. Pick a worker by its Role / Rank when the
    question is about one group's processes rather than the cluster's.

    \b
    Examples:
        inspire ray shell av-pipeline --workspace CPU资源空间
        inspire ray shell av-pipeline --workspace CPU资源空间 --instance decode-0
    """
    try:
        session = get_web_session()
        ray_job_id, instances = _run_readonly_ray_operation(
            ctx,
            session=session,
            name=name,
            workspace=workspace,
            limit=200,
            pick=pick,
            operation=lambda resolved_id: (
                resolved_id,
                _fetch_ray_instances(
                    resolved_id, limit=200, session=session, show_all=True
                )[0],
            ),
        )

        running = [
            row
            for row in instances
            if "run" in str(row.get("status") or row.get("instance_status") or "").lower()
        ]
        views = ray_instance_views(running)
        if not views:
            _handle_error(
                ctx,
                "ValidationError",
                "No running instances found for this Ray job.",
                EXIT_VALIDATION_ERROR,
            )
            return

        if instance:
            selected = select_ray_instance_views(views, [instance])[0]
        else:
            heads = [view for view in views if view.kind.lower() == "head"]
            selected = (heads or views)[0]

        if not ctx.json_output:
            click.echo(
                f"Opening shell: {scrub_raw_ids(name)} / {selected.label}", err=True
            )
            click.echo("Press Ctrl-] to disconnect.", err=True)

        sys.exit(
            open_job_shell(
                job_id=ray_job_id,
                instance_name=selected.handle,
                session=session,
                workload="ray",
            )
        )
    except RayInstanceSelectionError as e:
        _handle_error(ctx, "ValidationError", scrub_raw_ids(e), EXIT_VALIDATION_ERROR)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
    except JobShellError as e:
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
