"""Ray (弹性计算) job commands for Inspire CLI."""

from __future__ import annotations

import logging
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
from inspire.cli.utils.collection_output import (
    DEFAULT_COLLECTION_LIMIT,
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.events import run_events_command
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
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.task_priority import (
    TaskPriorityError,
    resolve_workspace_task_priority,
    task_priority_option,
)
from inspire.config import Config, ConfigError
from inspire.config.workload_profiles import apply_workload_profile, profile_required_message
from inspire.config.workspaces import select_workspace_id, workspace_label
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session

_DEFAULT_INSTANCE_SCAN_LIMIT = 500
logger = logging.getLogger(__name__)

IMAGE_TYPE_CHOICES = ["SOURCE_PUBLIC", "SOURCE_PRIVATE", "SOURCE_OFFICIAL"]


def _current_user_id(session) -> str:  # noqa: ANN001
    me = browser_api_module.get_current_user(session=session)
    user_id = str(me.get("id") or me.get("user_id") or "").strip()
    if not user_id:
        raise ValueError("Cannot determine the current user from the live web session.")
    return user_id


def _created_ray_job_id(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("ray_job_id", "job_id", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    for key in ("ray_job", "job", "data", "result"):
        value = _created_ray_job_id(payload.get(key))
        if value:
            return value
    return ""


def _resolve_ray_name_in_workspace(
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
        json_output=ctx.json_output,
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
    config: Config,
    session,
    name: str,
    workspace: str,
    limit: int,
    operation,
):
    """Run a read-only Ray operation and recover one stale cache hit."""
    def _resolve(require_live: bool) -> str:
        return _resolve_ray_name_in_workspace(
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
    """Keep useful Ray results while hiding platform handles and metadata."""
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


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _format_ray_list_rows(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "No Ray jobs found."

    name_w = max(len("Name"), *(len(r["name"]) for r in rows))
    status_w = max(len("Status"), *(len(r["status"]) for r in rows))
    created_w = max(len("Created"), *(len(r["created_at"]) for r in rows))
    user_w = max(len("Created By"), *(len(r["created_by_name"]) for r in rows))

    header = (
        f"{'Name':<{name_w}}  "
        f"{'Status':<{status_w}}  {'Created':<{created_w}}  "
        f"{'Created By':<{user_w}}"
    )
    sep = "-" * len(header)
    lines = ["Ray Jobs (弹性计算)", header, sep]
    for row in rows:
        lines.append(
            f"{row['name']:<{name_w}}  "
            f"{row['status']:<{status_w}}  "
            f"{row['created_at']:<{created_w}}  "
            f"{row['created_by_name']:<{user_w}}"
        )
    lines.append(sep)
    lines.append(f"Total: {len(rows)}")
    return "\n".join(lines)


def _ray_instance_name(inst: dict[str, Any], idx: int) -> str:
    for key in ("name", "instance_name", "pod_name"):
        value = str(inst.get(key) or "").strip()
        if value:
            return scrub_raw_ids(value)
    return f"#{idx}"


def _format_ray_instances(instances: list[dict[str, Any]]) -> str:
    if not instances:
        return "No Ray instances found."

    rendered = []
    for idx, inst in enumerate(instances, start=1):
        cpu = inst.get("cpu_count") or 0
        gpu = inst.get("gpu_count") or 0
        mem = inst.get("memory_size") or inst.get("memory_size_gib") or 0
        rendered.append(
            {
                "name": _ray_instance_name(inst, idx),
                "status": scrub_raw_ids(inst.get("status") or inst.get("instance_status") or ""),
                "type": scrub_raw_ids(inst.get("instance_type") or ""),
                "group": scrub_raw_ids(inst.get("worker_group_name") or ""),
                "resource": f"{cpu}C/{gpu}G/{mem}GiB",
                "created": human_formatter.format_epoch(inst.get("created_at")),
            }
        )

    name_w = max(len("Instance"), *(len(row["name"]) for row in rendered))
    status_w = max(len("Status"), *(len(row["status"]) for row in rendered))
    type_w = max(len("Type"), *(len(row["type"]) for row in rendered))
    group_w = max(len("Group"), *(len(row["group"]) for row in rendered))
    header = (
        f"{'Instance':<{name_w}} {'Status':<{status_w}} "
        f"{'Type':<{type_w}} {'Group':<{group_w}} {'Resource':<14} Created"
    )
    sep = "-" * len(header)
    lines = ["Ray Instances", header, sep]
    for row in rendered:
        lines.append(
            f"{row['name']:<{name_w}} "
            f"{row['status']:<{status_w}} "
            f"{row['type']:<{type_w}} "
            f"{row['group']:<{group_w}} "
            f"{row['resource']:<14} "
            f"{row['created']}"
        )
    lines.append(sep)
    lines.append(f"Total: {len(instances)} instance(s)")
    return "\n".join(lines)


def _fetch_ray_instances(
    ray_job_id: str,
    *,
    limit: int,
    session,
    show_all: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch the bounded instance page, expanding it only for explicit ``--all``."""
    rows, total = browser_api_module.list_ray_job_instances(
        ray_job_id,
        limit=limit,
        session=session,
    )
    if show_all and total > len(rows):
        expanded_rows, expanded_total = browser_api_module.list_ray_job_instances(
            ray_job_id,
            limit=max(total, len(rows), 1),
            session=session,
        )
        rows = expanded_rows
        total = max(total, expanded_total, len(rows))
    return rows, total


@click.command("list")
@click.option("--workspace", required=True, help="Workspace name")
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
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List Ray (弹性计算) jobs in a workspace."""
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

        me = browser_api_module.get_current_user(session=session)
        current_user_id = str(me.get("id") or me.get("user_id") or "").strip()
        if not current_user_id:
            raise ValueError("Cannot determine the current user from the live web session.")
        user_ids: Optional[list[str]] = [current_user_id]

        jobs, total = browser_api_module.list_ray_jobs(
            workspace_id=resolved_workspace_id,
            user_ids=user_ids,
            page_num=1,
            page_size=request_limit,
            session=session,
        )
        if show_all and total > len(jobs):
            jobs, expanded_total = browser_api_module.list_ray_jobs(
                workspace_id=resolved_workspace_id,
                user_ids=user_ids,
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
                "created_by_name": scrub_raw_ids(job.created_by_name or "N/A"),
                "project_name": scrub_raw_ids(job.project_name or ""),
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
@click.argument("name")
@click.option("--workspace", required=True, help="Workspace name.")
@pass_context
def status_ray(ctx: Context, name: str, workspace: str) -> None:
    """Show details for a Ray (弹性计算) job.

    NAME is the Ray job name shown in `inspire ray list`. Plain output shows
    the top-level status fields; use ``--json`` only when a script needs the
    full structured response.
    """
    name = _reject_ray_name_at_boundary(ctx, name)
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        data = _run_readonly_ray_operation(
            ctx,
            config=config,
            session=session,
            name=name,
            workspace=workspace,
            limit=10000,
            operation=lambda ray_job_id, live_session: (
                browser_api_module.get_ray_job_detail(
                    ray_job_id,
                    session=live_session,
                )
            ),
        )

        if ctx.json_output:
            click.echo(json_formatter.format_json(_public_output(data)))
            return

        click.echo("Ray Job Status")
        click.echo(f"Name:       {scrub_raw_ids(data.get('name', 'N/A'))}")
        click.echo(f"Status:     {scrub_raw_ids(data.get('status', 'N/A'))}")
        if data.get("sub_status"):
            click.echo(f"Sub:        {scrub_raw_ids(data.get('sub_status'))}")
        if data.get("priority") is not None:
            click.echo(f"Priority:   {data.get('priority')}")
        if data.get("priority_level"):
            click.echo(f"Priority Level: {scrub_raw_ids(data.get('priority_level'))}")
        created_by = data.get("created_by") or {}
        if created_by.get("name"):
            click.echo(f"Created By: {scrub_raw_ids(created_by.get('name'))}")
        if data.get("project_name"):
            click.echo(f"Project:    {scrub_raw_ids(data.get('project_name'))}")
        if data.get("created_at"):
            click.echo(f"Created:    {scrub_raw_ids(data.get('created_at'))}")
        if data.get("finished_at"):
            click.echo(f"Finished:   {scrub_raw_ids(data.get('finished_at'))}")

    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@click.command("stop")
@click.argument("name")
@click.option("--workspace", required=True, help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help="Pick the Nth candidate (1-indexed) when the name is ambiguous — "
    "matches the list order in the AmbiguousName error.",
)
@pass_context
def stop_ray(ctx: Context, name: str, workspace: str, pick: Optional[int]) -> None:
    """Stop a running Ray (弹性计算) job."""
    name = _reject_ray_name_at_boundary(ctx, name)
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        ray_job_id = _resolve_ray_name_in_workspace(
            ctx,
            config=config,
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
                json_formatter.format_json({"name": name, "stopped": True}),
            )
            return
        click.echo(human_formatter.format_success(f"Ray job stopped: {name}"))

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


def _project_label(config: Config, project_id: str, requested: Optional[str]) -> str:
    if requested:
        return project_display_name(config, requested)
    return "(project name unavailable)"


def _resolve_image_id(raw: str, *, session, ctx: Context) -> str:
    """Turn a visible image name or Docker image URL into the internal mirror handle.

    Ray's create body takes an internal mirror handle, not the pullable Docker
    URL. We walk public + private + official image catalogues looking for an
    exact URL/name match.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ConfigError("Image is empty.")
    target = raw.lower()
    for source in ("private", "public", "official"):
        try:
            images = browser_api_module.list_images_by_source(source=source, session=session)
        except Exception:  # noqa: BLE001
            if ctx.debug:
                logger.debug("Ray image lookup via %s failed", source, exc_info=True)
            continue
        for img in images:
            labels = {
                str(img.url or "").strip(),
                str(img.name or "").strip(),
            }
            if img.name and img.version:
                labels.add(f"{img.name}:{img.version}")
            if target in {label.lower() for label in labels if label}:
                return img.image_id
    display = scrub_raw_ids(raw)
    raise ConfigError(
        f"Image {display!r} not found in public/private/official catalogues. "
        "Pass a visible image name or Docker URL from `inspire image list`."
    )


def _parse_worker_spec(raw: str) -> dict[str, Any]:
    """Parse a ``key=value;key=value`` worker spec into a dict.

    Required keys: ``name``, ``image`` (visible image name or URL), ``group`` (compute
    group name), ``quota`` (``gpu,cpu,mem`` triple), ``min``, ``max``.
    Optional: ``image-type`` (default SOURCE_PUBLIC), ``shm-size`` (shm_gi).

    Tokens are separated by ``;`` so the ``,`` inside ``quota=4,80,800``
    doesn't collide with the outer separator.
    """
    from inspire.cli.utils.quota_resolver import QuotaParseError, parse_quota

    out: dict[str, Any] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise click.BadParameter(f"worker spec token {chunk!r} has no '='; expected key=value")
        k, _, v = chunk.partition("=")
        out[k.strip()] = v.strip()

    missing = {"name", "image", "group", "quota", "min", "max"} - out.keys()
    if missing:
        raise click.BadParameter(
            f"worker spec missing keys: {sorted(missing)}. "
            "Required: name, image, group, quota, min, max. Optional: image-type, shm-size. "
            "Format: 'name=...;image=...;group=...;quota=gpu,cpu,mem;min=N;max=N'."
        )
    try:
        out["quota_spec"] = parse_quota(out["quota"])
    except QuotaParseError as e:
        raise click.BadParameter(f"worker quota: {e}")
    try:
        out["min"] = int(out["min"])
        out["max"] = int(out["max"])
    except ValueError as e:
        raise click.BadParameter(f"min/max must be integers: {e}")
    if out["min"] < 1 or out["max"] < 1:
        raise click.BadParameter("worker min and max must be >= 1.")
    if out["max"] < out["min"]:
        raise click.BadParameter("worker max must be >= min.")
    if "image_type" in out or "shm" in out:
        raise click.BadParameter("Use worker keys image-type and shm-size, not image_type or shm.")
    image_type = str(out.get("image-type") or "SOURCE_PUBLIC").strip()
    if image_type not in IMAGE_TYPE_CHOICES:
        raise click.BadParameter(
            "worker image-type must be one of "
            + ", ".join(IMAGE_TYPE_CHOICES)
            + "."
        )
    out["image_type"] = image_type
    out.pop("image-type", None)
    if "shm-size" in out and out["shm-size"] not in ("", None):
        try:
            out["shm_size"] = int(out["shm-size"])
        except ValueError as e:
            raise click.BadParameter(f"shm-size must be an integer GiB value: {e}")
        if out["shm_size"] < 1:
            raise click.BadParameter("worker shm-size must be >= 1.")
    else:
        out.pop("shm_size", None)
    out.pop("shm-size", None)
    return out


@click.command("create")
@click.option("--name", "-n", required=True, help="Ray job name")
@click.option(
    "--command",
    "-c",
    required=True,
    help="Driver startup command. The Ray job stays alive while this command keeps running.",
)
@click.option("--description", default="", help="Free-form description")
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
    help="Ray condition profile for workspace/project/group/quota/image.",
)
@task_priority_option()
@click.option(
    "--image",
    default=None,
    help="Head node image name or Docker URL. Required unless supplied by --profile.",
)
@click.option(
    "--image-type",
    type=click.Choice(IMAGE_TYPE_CHOICES),
    default="SOURCE_PUBLIC",
    show_default=True,
    help="Head node image source type.",
)
@click.option(
    "--group",
    default=None,
    help=(
        "Full compute group name copied from the same quota row as --quota. "
        "Required unless supplied by --profile."
    ),
)
@click.option(
    "--quota",
    default=None,
    help=(
        "Head node resource quota as 'gpu,cpu,mem' (mem in GiB). "
        "CLI resolves the triple against 'inspire ray quota --workspace <name>'."
    ),
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
    help=(
        "Worker group spec (repeatable). Format (note ';' separator): "
        "'name=<grp>;image=<url-or-name>;group=<full-group-name>;quota=<gpu,cpu,mem>;"
        "min=<n>;max=<n>[;image-type=SOURCE_PUBLIC][;shm-size=<gib>]'"
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
    profile_name: Optional[str],
    priority: Optional[int],
    image: Optional[str],
    image_type: str,
    group: Optional[str],
    quota: Optional[str],
    shm_size: Optional[int],
    workers: tuple[str, ...],
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
          --project CI-情境智能 \\
          --image ray-base:v1 \\
          --group HPC-可上网区资源-2 --quota 0,4,16 \\
          --worker 'name=decode;image=ray-base:v1;group=HPC-可上网区资源-2;quota=0,20,80;min=1;max=8;shm-size=32'

    """
    try:
        config, _ = Config.from_files_and_env()
        session = get_web_session()

        fields = apply_workload_profile(
            profiles=getattr(config, "profiles", {}),
            kind="ray",
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
        )

        if dry_run:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json(
                        {
                            "dry_run": True,
                            "name": body.get("name"),
                            "description": body.get("description"),
                            "entrypoint": body.get("entrypoint"),
                            "worker_groups": _public_output(body.get("worker_groups") or []),
                        }
                    )
                )
                return
            click.echo("Ray create request preview")
            click.echo(f"Name:      {scrub_raw_ids(body.get('name'))}")
            click.echo(
                f"Project:   {scrub_raw_ids(_project_label(config, body.get('project_id', ''), project))}"
            )
            click.echo(
                f"Workspace: {scrub_raw_ids(workspace_label(session, body.get('workspace_id', ''), workspace))}"
            )
            click.echo(f"Workers:   {len(body.get('worker_groups') or [])} group(s)")
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
            click.echo(json_formatter.format_json(_public_output(data)))
            return

        click.echo(human_formatter.format_success(f"Ray job created: {body.get('name')}"))
        click.echo(
            f"Project:   {scrub_raw_ids(_project_label(config, body.get('project_id', ''), project))}"
        )
        click.echo(
            f"Workspace: {scrub_raw_ids(workspace_label(session, body.get('workspace_id', ''), workspace))}"
        )
        click.echo(f"Workers:   {len(body.get('worker_groups') or [])} group(s)")

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
) -> dict[str, Any]:
    from inspire.cli.utils.quota_resolver import (
        QuotaMatchError,
        QuotaParseError,
        SCHEDULE_TYPE_RAY,
        parse_quota,
        resolve_quota,
    )

    if not name:
        raise click.UsageError("--name is required.")
    if not command:
        raise click.UsageError(
            "--command is required; it is the Ray driver startup command."
        )
    for field_name, value in (
        ("image", image),
        ("group", group),
        ("quota", quota),
        ("workspace", workspace),
        ("project", project),
    ):
        if not value:
            raise click.UsageError(profile_required_message("ray", field_name))
    image_value = cast(str, image)
    image_type_value = image_type.strip()
    if image_type_value not in IMAGE_TYPE_CHOICES:
        raise click.UsageError(
            f"--image-type must be one of: {', '.join(IMAGE_TYPE_CHOICES)}"
        )
    group_value = cast(str, group)
    quota_value = cast(str, quota)
    if not workers:
        raise click.UsageError(
            "At least one --worker is required. Format: "
            "'name=<g>;image=<u>;group=<g>;quota=<gpu,cpu,mem>;min=<n>;max=<n>'"
        )

    resolved_workspace_id = select_workspace_id(
        config,
        explicit_workspace_name=workspace,
        session=session,
    )
    if resolved_workspace_id is None:
        raise ConfigError(profile_required_message("ray", "workspace"))
    resolved_project_id = _resolve_project_id(
        config,
        project,
        workspace_id=resolved_workspace_id,
        session=session,
        ctx=ctx,
    )

    def _resolve_ray(triple: str, group_name: str) -> Any:
        try:
            spec_triple = parse_quota(triple)
        except QuotaParseError as exc:
            raise click.UsageError(str(exc)) from exc
        try:
            return resolve_quota(
                spec=spec_triple,
                workspace_id=resolved_workspace_id,
                session=session,
                schedule_config_type=SCHEDULE_TYPE_RAY,
                group_override=group_name,
            )
        except QuotaMatchError as exc:
            raise click.UsageError(str(exc)) from exc

    head_resolved = _resolve_ray(quota_value, group_value)
    head_node: dict[str, Any] = {
        "mirror_id": _resolve_image_id(image_value, session=session, ctx=ctx),
        "image_type": image_type_value,
        "logic_compute_group_id": head_resolved.logic_compute_group_id,
        "quota_id": head_resolved.quota_id,
    }
    if shm_size is not None:
        head_node["shm_gi"] = shm_size

    worker_groups: list[dict[str, Any]] = []
    for raw in workers:
        spec = _parse_worker_spec(raw)
        worker_resolved = _resolve_ray(spec["quota"], spec["group"])
        group_block: dict[str, Any] = {
            "group_name": spec["name"],
            "mirror_id": _resolve_image_id(spec["image"], session=session, ctx=ctx),
            "image_type": spec["image_type"],
            "logic_compute_group_id": worker_resolved.logic_compute_group_id,
            "min_replicas": spec["min"],
            "max_replicas": spec["max"],
            "quota_id": worker_resolved.quota_id,
        }
        if "shm_size" in spec:
            group_block["shm_gi"] = spec["shm_size"]
        worker_groups.append(group_block)

    body: dict[str, Any] = {
        "name": name,
        "description": description,
        "workspace_id": resolved_workspace_id,
        "project_id": resolved_project_id,
        "entrypoint": command,
        "head_node": head_node,
        "worker_groups": worker_groups,
    }
    body["task_priority"] = resolve_workspace_task_priority(
        priority,
        session=session,
        workspace_id=resolved_workspace_id,
        project_id=resolved_project_id,
    )
    return body


_RAY_EVENT_NAME_SCAN_LIMIT = 500
_RAY_EVENT_PAGE_SIZE = 200
_RAY_EVENT_MAX_PAGES = 5


def _fetch_recent_ray_events(ray_job_id: str, *, session) -> list[dict]:  # noqa: ANN001
    """Fetch a bounded newest-first window and restore chronological output."""
    events = browser_api_module.list_ray_job_events(
        ray_job_id,
        page_size=_RAY_EVENT_PAGE_SIZE,
        max_pages=_RAY_EVENT_MAX_PAGES,
        sort_ascending=False,
        session=session,
    )
    return list(reversed(events))


@click.command("events")
@click.argument("name")
@click.option("--workspace", required=True, help="Workspace name.")
@click.option(
    "--tail",
    type=click.IntRange(1),
    default=20,
    show_default=True,
    help="Maximum recent events to display.",
)
@click.option(
    "--reason",
    default=None,
    help="Filter by event reason (e.g. FailedScheduling, CreatedRayCluster).",
)
@click.option(
    "--type",
    "type_filter",
    type=click.Choice(["Normal", "Warning"], case_sensitive=False),
    default=None,
    help="Filter by event type.",
)
@click.option("--follow", "-f", is_flag=True, help="Follow the event timeline and print new events.")
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
    tail: Optional[int],
    reason: Optional[str],
    type_filter: Optional[str],
    follow: bool,
    interval: int,
) -> None:
    """Show events for a Ray (弹性计算) job.

    \b
    Critical for diagnosing stuck PENDING jobs — the `FailedScheduling`
    events spell out exactly why the scheduler can't place a pod
    (insufficient CPU / GPU, node affinity mismatch, taint, etc.).

    \b
    Examples:
        inspire ray events <ray-name> --workspace CPU资源空间
        inspire ray events <ray-name> --workspace CPU资源空间 --reason FailedScheduling
        inspire ray events <ray-name> --workspace CPU资源空间 --type Warning --tail 10
        inspire ray events <ray-name> --workspace CPU资源空间 --follow
        inspire --json ray events <ray-name> --workspace CPU资源空间
    """
    name = _reject_ray_name_at_boundary(ctx, name)
    try:
        session = get_web_session()
        config, _ = Config.from_files_and_env(require_credentials=False)
        run_events_command(
            ctx,
            resource_id=name,
            resource_type="ray",
            resource_name=name,
            fetch=lambda: _run_readonly_ray_operation(
                ctx,
                config=config,
                session=session,
                name=name,
                workspace=workspace,
                limit=_RAY_EVENT_NAME_SCAN_LIMIT,
                operation=lambda ray_job_id, live_session: (
                    _fetch_recent_ray_events(
                        ray_job_id,
                        session=live_session,
                    )
                ),
            ),
            json_output_local=False,
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
def instances_ray(
    ctx: Context,
    name: str,
    workspace: str,
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List pod-level instances (head + workers) for a Ray job.

    \b
    NAME is resolved within the explicit workspace and current live user.
    Shows each pod's status; check `inspire ray events <name> --workspace <workspace>`
    for scheduler reasons when pods remain pending.
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
            config=config,
            session=session,
            name=name,
            workspace=workspace,
            limit=resolution_limit,
            operation=lambda ray_job_id, live_session: _fetch_ray_instances(
                ray_job_id,
                limit=request_limit,
                session=live_session,
                show_all=show_all,
            ),
        )
        page = bound_collection(instances, limit=output_limit, total=total)

        if ctx.json_output:
            payload: dict[str, Any] = {
                "instances": _public_output(page.items),
                "total": page.total,
            }
            if page.truncated:
                payload.update(page.metadata())
                payload["limit"] = output_limit
            click.echo(
                json_formatter.format_json(payload)
            )
            return

        click.echo(_format_ray_instances(page.items))
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
def delete_ray(ctx: Context, name: str, workspace: str, yes: bool, pick: Optional[int]) -> None:
    """Permanently delete a Ray (弹性计算) job record.

    \b
    The entry disappears from the platform Ray list. This cannot be undone; if the
    job is still running, `stop` it first so the scheduler releases
    reserved capacity cleanly.
    """
    name = _reject_ray_name_at_boundary(ctx, name)
    if not yes and not ctx.json_output:
        click.confirm(
            f"Permanently delete Ray job '{scrub_raw_ids(name)}'? This cannot be undone.",
            abort=True,
        )

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        ray_job_id = _resolve_ray_name_in_workspace(
            ctx,
            config=config,
            session=session,
            name=name,
            workspace=workspace,
            limit=10000,
            pick=pick,
            require_live=True,
        )
        browser_api_module.delete_ray_job(ray_job_id, session=session)
        workspace_id = select_workspace_id(
            config,
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
        click.echo(human_formatter.format_success(f"Ray job deleted: {name}"))

    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
