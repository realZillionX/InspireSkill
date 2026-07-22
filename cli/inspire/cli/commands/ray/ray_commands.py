"""Ray (弹性计算) job commands for Inspire CLI."""

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
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.events import run_events_command
from inspire.cli.utils.id_resolver import reject_id_at_boundary, resolve_by_name
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
    config: Config,
    session,
    name: str,
    workspace: str,
    limit: int,
    pick: Optional[int] = None,
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
    )


def _reject_ray_name_at_boundary(ctx: Context, name: str) -> str:
    return reject_id_at_boundary(
        ctx,
        name,
        resource_type="ray",
        list_command="inspire ray list --workspace <workspace>",
    )


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
    for key in ("name", "instance_name", "pod_name", "instance_id"):
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


@click.command("list")
@click.option("--workspace", required=True, help="Workspace name")
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=50,
    show_default=True,
    help="Maximum Ray jobs to query and display.",
)
@pass_context
def list_ray(
    ctx: Context,
    workspace: Optional[str],
    limit: int,
) -> None:
    """List Ray (弹性计算) jobs in a workspace."""
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
            page_size=limit,
            session=session,
        )
        rows = [
            {
                "ray_job_id": job.ray_job_id or "N/A",
                "name": scrub_raw_ids(job.name or "N/A"),
                "status": scrub_raw_ids(job.status or "N/A"),
                "created_at": scrub_raw_ids(job.created_at or "N/A"),
                "created_by_name": scrub_raw_ids(job.created_by_name or "N/A"),
                "created_by_id": job.created_by_id or "",
                "project_name": scrub_raw_ids(job.project_name or ""),
                "project_id": job.project_id or "",
                "workspace_id": job.workspace_id or "",
            }
            for job in jobs
        ]

        if ctx.json_output:
            click.echo(
                json_formatter.format_json({"jobs": rows, "total": total}),
            )
            return

        click.echo(_format_ray_list_rows(rows))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
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
        ray_job_id = _resolve_ray_name_in_workspace(
            ctx,
            config=config,
            session=session,
            name=name,
            workspace=workspace,
            limit=10000,
        )
        data = browser_api_module.get_ray_job_detail(ray_job_id, session=session)

        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
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
        click.echo(
            "\nUse `inspire --json ray status <name>` to see full head / worker "
            "spec and elastic instance ranges."
        )

    except (SessionExpiredError, ValueError) as e:
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
        )
        browser_api_module.stop_ray_job(ray_job_id, session=session)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json({"name": name, "stopped": True}),
            )
            return
        click.echo(human_formatter.format_success(f"Ray job stopped: {name}"))

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def _resolve_project_id(config: Config, requested: Optional[str]) -> str:
    """Resolve a project name to the underlying project_id."""
    if requested:
        if requested.startswith("project-"):
            raise ConfigError(
                "--project takes a project name. "
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
    raise ConfigError("--project is required.")


def _project_label(config: Config, project_id: str, requested: Optional[str]) -> str:
    if requested:
        return requested
    for name, candidate in (config.projects or {}).items():
        if candidate == project_id:
            return name
    entry = (config.project_catalog or {}).get(project_id)
    if isinstance(entry, dict) and entry.get("name"):
        return str(entry["name"])
    return "(project name unavailable)"


def _resolve_image_id(raw: str, *, session, ctx: Context) -> str:
    """Turn a visible image name or Docker image URL into a mirror_id.

    Ray's create body takes ``mirror_id`` (the platform's internal image id),
    not the pullable Docker URL. We walk public + private + official image
    catalogues looking for an exact URL/name match.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ConfigError("Image is empty.")
    target = raw.lower()
    for source in ("private", "public", "official"):
        try:
            images = browser_api_module.list_images_by_source(source=source, session=session)
        except Exception as e:  # noqa: BLE001
            if ctx.debug:
                click.echo(f"  image lookup via {source} failed: {e}", err=True)
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

    Required keys: ``name``, ``image`` (URL or image_id), ``group`` (compute
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
                click.echo(json_formatter.format_json({"dry_run": True, "payload": body}))
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

        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return

        click.echo(human_formatter.format_success(f"Ray job created: {body.get('name')}"))
        click.echo(
            f"Project:   {scrub_raw_ids(_project_label(config, body.get('project_id', ''), project))}"
        )
        click.echo(
            f"Workspace: {scrub_raw_ids(workspace_label(session, body.get('workspace_id', ''), workspace))}"
        )
        click.echo(f"Workers:   {len(body.get('worker_groups') or [])} group(s)")
        sub_msg = data.get("sub_msg") or ""
        if sub_msg:
            click.echo(f"Platform note: {scrub_raw_ids(sub_msg)}")

    except TaskPriorityError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except click.UsageError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
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

    resolved_project_id = _resolve_project_id(config, project)
    resolved_workspace_id = select_workspace_id(
        config,
        explicit_workspace_name=workspace,
        session=session,
    )
    if resolved_workspace_id is None:
        raise ConfigError(profile_required_message("ray", "workspace"))

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


@click.command("events")
@click.argument("name")
@click.option("--workspace", required=True, help="Workspace name.")
@click.option(
    "--tail",
    type=click.IntRange(1),
    default=None,
    help="Show only the most recent N events (default: all).",
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
        ray_job_id = _resolve_ray_name_in_workspace(
            ctx,
            config=config,
            session=session,
            name=name,
            workspace=workspace,
            limit=10000,
        )
        run_events_command(
            ctx,
            resource_id=ray_job_id,
            resource_type="ray",
            resource_name=name,
            fetch=lambda: browser_api_module.list_ray_job_events(ray_job_id, session=session),
            json_output_local=False,
            type_filter=type_filter,
            reason_filter=reason,
            tail=tail,
            follow=follow,
            interval=interval,
        )

    except (SessionExpiredError, ValueError) as e:
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
    default=500,
    show_default=True,
    help="Maximum Ray jobs to scan while resolving the name and maximum instances to query.",
)
@pass_context
def instances_ray(ctx: Context, name: str, workspace: str, limit: int) -> None:
    """List pod-level instances (head + workers) for a Ray job.

    \b
    NAME is resolved within the explicit workspace and current live user.
    Shows each pod's status; check `inspire ray events <name> --workspace <workspace>`
    for scheduler reasons when pods remain pending.
    """
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
            limit=limit,
        )
        instances, total = browser_api_module.list_ray_job_instances(
            ray_job_id,
            limit=limit,
            session=session,
        )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "source": "web",
                        "ray_job_id": ray_job_id,
                        "instances": instances,
                        "total": total,
                    }
                )
            )
            return

        click.echo(_format_ray_instances(instances))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
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
        )
        browser_api_module.delete_ray_job(ray_job_id, session=session)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json({"name": name, "status": "deleted"}),
            )
            return
        click.echo(human_formatter.format_success(f"Ray job deleted: {name}"))

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
