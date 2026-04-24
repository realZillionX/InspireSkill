"""Ray (弹性计算) job commands for Inspire CLI."""

from __future__ import annotations

import json
from pathlib import Path
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
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import resolve_by_name
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session


def _resolve_ray_name(ctx: Context, name: str, *, pick: Optional[int] = None) -> str:
    """Resolve a Ray job name to its platform id (``rj-<uuid>``).

    v2.0.0: names only. Candidate set is restricted to the current user's
    Ray jobs in the session's workspace — avoids cross-user same-name
    ambiguity (you wouldn't have permission to operate on a teammate's
    job anyway). Full-page fetch (`page_size=10000`) so large workspaces
    don't drop the target off the end of page 1.
    """
    def _lister():
        session = get_web_session()
        me = browser_api_module.get_current_user(session=session)
        uid = str(me.get("id") or me.get("user_id") or "").strip()
        user_ids = [uid] if uid else None  # keep widen-behaviour as a last resort
        jobs, _ = browser_api_module.list_ray_jobs(
            session=session, user_ids=user_ids, page_size=10000
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


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _format_ray_list_rows(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "No Ray jobs found."

    id_w = max(len("Ray Job ID"), *(len(r["ray_job_id"]) for r in rows))
    name_w = max(len("Name"), *(len(r["name"]) for r in rows))
    status_w = max(len("Status"), *(len(r["status"]) for r in rows))
    created_w = max(len("Created"), *(len(r["created_at"]) for r in rows))
    user_w = max(len("Created By"), *(len(r["created_by_name"]) for r in rows))

    header = (
        f"{'Ray Job ID':<{id_w}} {'Name':<{name_w}} "
        f"{'Status':<{status_w}} {'Created':<{created_w}} "
        f"{'Created By':<{user_w}}"
    )
    sep = "-" * len(header)
    lines = ["Ray Jobs (弹性计算)", header, sep]
    for row in rows:
        lines.append(
            f"{row['ray_job_id']:<{id_w}} "
            f"{row['name']:<{name_w}} "
            f"{row['status']:<{status_w}} "
            f"{row['created_at']:<{created_w}} "
            f"{row['created_by_name']:<{user_w}}"
        )
    lines.append(sep)
    lines.append(f"Total: {len(rows)}")
    return "\n".join(lines)


@click.command("list")
@click.option("--workspace", default=None, help="Workspace name (from [workspaces])")
@click.option(
    "--all-users",
    "-A",
    is_flag=True,
    help="Include jobs from all users (default: only the current user).",
)
@click.option(
    "--created-by",
    "created_by",
    default=None,
    help="Filter by creator user ID (user-…); repeatable-friendly comma-separated list.",
)
@click.option("--page-num", type=int, default=1, show_default=True, help="Page number")
@click.option("--page-size", type=int, default=20, show_default=True, help="Page size")
@pass_context
def list_ray(
    ctx: Context,
    workspace: Optional[str],
    all_users: bool,
    created_by: Optional[str],
    page_num: int,
    page_size: int,
) -> None:
    """List Ray (弹性计算) jobs in a workspace."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        resolved_workspace_id = None
        if workspace is not None:
            resolved_workspace_id = select_workspace_id(
                config,
                explicit_workspace_name=workspace,
            )

        session = get_web_session()

        user_ids: Optional[list[str]] = None
        if all_users:
            user_ids = None
        elif created_by:
            user_ids = [uid.strip() for uid in created_by.split(",") if uid.strip()]
        else:
            # Default: scope to the logged-in user, mirroring the web UI's
            # "我的" tab so a shared workspace doesn't dump everyone's jobs.
            try:
                me = browser_api_module.get_current_user(session=session)
                current_user_id = str(me.get("id") or me.get("user_id") or "").strip()
                if current_user_id:
                    user_ids = [current_user_id]
            except Exception:
                user_ids = None

        jobs, total = browser_api_module.list_ray_jobs(
            workspace_id=resolved_workspace_id,
            user_ids=user_ids,
            page_num=page_num,
            page_size=page_size,
            session=session,
        )
        rows = [
            {
                "ray_job_id": job.ray_job_id or "N/A",
                "name": job.name or "N/A",
                "status": job.status or "N/A",
                "created_at": job.created_at or "N/A",
                "created_by_name": job.created_by_name or "N/A",
                "created_by_id": job.created_by_id or "",
                "project_name": job.project_name or "",
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
@pass_context
def status_ray(ctx: Context, name: str) -> None:
    """Show details for a Ray (弹性计算) job.

    NAME is the Ray job name shown in `inspire ray list`. The Ray detail
    payload nests head + worker specs and elastic instance ranges;
    ``--json`` surfaces the full structure, plain output shows the
    top-level status fields.
    """
    try:
        ray_job_id = _resolve_ray_name(ctx, name)
        session = get_web_session()
        data = browser_api_module.get_ray_job_detail(ray_job_id, session=session)

        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return

        click.echo("Ray Job Status")
        click.echo(f"Name:       {data.get('name', 'N/A')}")
        click.echo(f"Status:     {data.get('status', 'N/A')}")
        if data.get("sub_status"):
            click.echo(f"Sub:        {data.get('sub_status')}")
        if data.get("priority") is not None:
            click.echo(f"Priority:   {data.get('priority')}")
        if data.get("priority_level"):
            click.echo(f"Priority Level: {data.get('priority_level')}")
        created_by = data.get("created_by") or {}
        if created_by.get("name"):
            click.echo(f"Created By: {created_by.get('name')}")
        if data.get("project_name"):
            click.echo(f"Project:    {data.get('project_name')}")
        if data.get("created_at"):
            click.echo(f"Created:    {data.get('created_at')}")
        if data.get("finished_at"):
            click.echo(f"Finished:   {data.get('finished_at')}")
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
@click.option(
    "--pick",
    type=int,
    default=None,
    help="Pick the Nth candidate (1-indexed) when the name is ambiguous — "
         "matches the list order in the AmbiguousName error.",
)
@pass_context
def stop_ray(ctx: Context, name: str, pick: Optional[int]) -> None:
    """Stop a running Ray (弹性计算) job."""
    try:
        ray_job_id = _resolve_ray_name(ctx, name, pick=pick)
        session = get_web_session()
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


def _resolve_compute_group_id(config: Config, requested: str) -> str:
    """Resolve a compute-group name to ``logic_compute_group_id``."""
    requested = (requested or "").strip()
    if not requested:
        raise ConfigError("Compute group cannot be empty.")
    if requested.startswith("lcg-"):
        raise ConfigError(
            f"compute-group takes a name, not a raw ID ({requested!r}). "
            "See `inspire config context` for available names."
        )
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
    raise ConfigError(f"Unknown compute group: {requested!r}. Available: {hint}")


def _resolve_image_id(raw: str, *, session, ctx: Context) -> str:
    """Turn a Docker image URL (or already-internal image_id) into a mirror_id.

    Ray's create body takes ``mirror_id`` (the platform's internal image id),
    not the pullable Docker URL. We walk public + private + official image
    catalogues looking for an exact URL match; if the caller already passed
    a known image_id (no slashes, no colon suffix), return it as-is.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ConfigError("Image is empty.")
    # Escape hatch — if this looks like a raw id (no slashes), trust it.
    if "/" not in raw and ":" not in raw:
        return raw

    for source in ("private", "public", "official"):
        try:
            images = browser_api_module.list_images_by_source(source=source, session=session)
        except Exception as e:  # noqa: BLE001
            if ctx.debug:
                click.echo(f"  image lookup via {source} failed: {e}", err=True)
            continue
        for img in images:
            if (img.url or "").strip() == raw:
                return img.image_id
    raise ConfigError(
        f"Image {raw!r} not found in public/private/official catalogues. "
        "Pass --head-image-id / --worker image_id=... directly if you already know it."
    )


def _parse_worker_spec(raw: str) -> dict[str, Any]:
    """Parse a ``key=value,key=value`` worker spec into a dict.

    Required keys: ``name``, ``image`` (URL or image_id), ``group`` (compute
    group name), ``spec`` (quota_id), ``min``, ``max``.
    Optional: ``image_type`` (default SOURCE_PUBLIC), ``shm`` (shm_gi).
    """
    out: dict[str, Any] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise click.BadParameter(
                f"worker spec token {chunk!r} has no '='; expected key=value"
            )
        k, _, v = chunk.partition("=")
        out[k.strip()] = v.strip()

    missing = {"name", "image", "group", "spec", "min", "max"} - out.keys()
    if missing:
        raise click.BadParameter(
            f"worker spec missing keys: {sorted(missing)}. "
            "Required: name, image, group, spec, min, max. Optional: image_type, shm."
        )
    try:
        out["min"] = int(out["min"])
        out["max"] = int(out["max"])
    except ValueError as e:
        raise click.BadParameter(f"min/max must be integers: {e}")
    if "shm" in out and out["shm"] not in ("", None):
        try:
            out["shm"] = int(out["shm"])
        except ValueError as e:
            raise click.BadParameter(f"shm must be an integer GiB value: {e}")
    else:
        out.pop("shm", None)
    out.setdefault("image_type", "SOURCE_PUBLIC")
    return out


@click.command("create")
@click.option("--name", "-n", help="Ray job name")
@click.option(
    "--command",
    "-c",
    help="Driver entrypoint command (maps to `entrypoint` on the wire)",
)
@click.option("--description", default="", help="Free-form description")
@click.option(
    "--project",
    "-p",
    default=None,
    help="Project name / alias / ID (default from [context].project)",
)
@click.option("--workspace", default=None, help="Workspace name (from [workspaces])")
@click.option(
    "--priority",
    type=click.IntRange(1, 10),
    default=None,
    help="Task priority 1-10 (platform convention: 1=LOW, 9/10=HIGH)",
)
@click.option(
    "--head-image",
    default=None,
    help="Head node image — Docker URL (will be resolved to mirror_id) or internal image_id",
)
@click.option(
    "--head-image-type",
    default="SOURCE_PUBLIC",
    show_default=True,
    help="SOURCE_PUBLIC / SOURCE_PRIVATE / SOURCE_OFFICIAL",
)
@click.option(
    "--head-group",
    default=None,
    help="Head compute group name; see 'inspire config context'",
)
@click.option(
    "--head-spec",
    default=None,
    help="Head quota_id (use 'inspire resources specs' to discover)",
)
@click.option(
    "--head-shm",
    type=int,
    default=None,
    help="Head shared memory in GiB (optional)",
)
@click.option(
    "--worker",
    "workers",
    multiple=True,
    help=(
        "Worker group spec (repeatable). Format: "
        "'name=<grp>,image=<url|id>,group=<group-or-lcg>,spec=<quota_id>,"
        "min=<n>,max=<n>[,image_type=SOURCE_PUBLIC][,shm=<gib>]'"
    ),
)
@click.option(
    "--json-body",
    "json_body_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Escape hatch: read the full create body (JSON) from file and POST "
        "verbatim. All other head/worker flags are ignored when set."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the assembled request body and exit without submitting.",
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
    head_image: Optional[str],
    head_image_type: str,
    head_group: Optional[str],
    head_spec: Optional[str],
    head_shm: Optional[int],
    workers: tuple[str, ...],
    json_body_path: Optional[Path],
    dry_run: bool,
) -> None:
    """Create a Ray (弹性计算) job with one head + one or more worker groups.

    \b
    Wire contract (reverse-engineered from the SPA's submit handler):
      POST /api/v1/ray_job/create  — head_node{...} + worker_groups[{...}]
      Image fields are `mirror_id` (internal id), not Docker URLs.
      Spec field is `quota_id` (notebook style), not `predef_quota_id`.
      Command is serialised as `entrypoint`.

    \b
    Example:
        inspire ray create \\
          -n av-pipeline \\
          -c 'python driver.py --mode run_and_exit' \\
          --head-image docker.sii.shaipower.online/inspire-studio/unified-base:v1 \\
          --head-group HPC-可上网区资源-2 --head-spec quota-head-abc \\
          --worker 'name=decode,image=docker.../cpu-decode:v1,group=HPC-可上网区资源-2,spec=quota-cpu-def,min=1,max=8,shm=32' \\
          --worker 'name=infer,image=docker.../gpu-infer:v1,group=分布式训练空间,spec=quota-gpu-xyz,min=1,max=2,image_type=SOURCE_PRIVATE' \\
          -p my-project

    \b
    Or escape-hatch the full body:
        inspire ray create --json-body body.json
    """
    try:
        config, _ = Config.from_files_and_env(require_target_dir=False)
        session = get_web_session()

        if json_body_path is not None:
            body = json.loads(json_body_path.read_text())
            if not isinstance(body, dict):
                raise click.UsageError("--json-body file must contain a JSON object.")
        else:
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
                head_image=head_image,
                head_image_type=head_image_type,
                head_group=head_group,
                head_spec=head_spec,
                head_shm=head_shm,
                workers=workers,
            )

        if dry_run:
            # Print the raw POST body so it can be piped back via --json-body.
            click.echo(json.dumps(body, indent=2, ensure_ascii=False))
            return

        data = browser_api_module.create_ray_job(body, session=session)

        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return

        click.echo(
            human_formatter.format_success(f"Ray job created: {body.get('name')}")
        )
        click.echo(f"Project:   {body.get('project_id')}")
        click.echo(f"Workspace: {body.get('workspace_id')}")
        click.echo(f"Workers:   {len(body.get('worker_groups') or [])} group(s)")
        sub_msg = data.get("sub_msg") or ""
        if sub_msg:
            click.echo(f"Platform note: {sub_msg}")

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
    head_image: Optional[str],
    head_image_type: str,
    head_group: Optional[str],
    head_spec: Optional[str],
    head_shm: Optional[int],
    workers: tuple[str, ...],
) -> dict[str, Any]:
    if not name:
        raise click.UsageError("--name is required (or use --json-body).")
    if not command:
        raise click.UsageError(
            "--command is required (the Ray driver entrypoint; wire field `entrypoint`)."
        )
    if not head_image or not head_group or not head_spec:
        raise click.UsageError(
            "Head node needs --head-image, --head-group, and --head-spec."
        )
    if not workers:
        raise click.UsageError(
            "At least one --worker is required. Format: "
            "'name=<g>,image=<u>,group=<g>,spec=<q>,min=<n>,max=<n>'"
        )

    resolved_project_id = _resolve_project_id(config, project)
    resolved_workspace_id = select_workspace_id(
        config,
        explicit_workspace_name=workspace,
    )
    if resolved_workspace_id is None:
        raise ConfigError(
            "Missing workspace_id. Set --workspace or configure [workspaces]."
        )

    head_node: dict[str, Any] = {
        "mirror_id": _resolve_image_id(head_image, session=session, ctx=ctx),
        "image_type": head_image_type,
        "logic_compute_group_id": _resolve_compute_group_id(config, head_group),
        "quota_id": head_spec,
    }
    if head_shm is not None:
        head_node["shm_gi"] = head_shm

    worker_groups: list[dict[str, Any]] = []
    for raw in workers:
        spec = _parse_worker_spec(raw)
        group_block: dict[str, Any] = {
            "group_name": spec["name"],
            "mirror_id": _resolve_image_id(spec["image"], session=session, ctx=ctx),
            "image_type": spec["image_type"],
            "logic_compute_group_id": _resolve_compute_group_id(config, spec["group"]),
            "min_replicas": spec["min"],
            "max_replicas": spec["max"],
            "quota_id": spec["spec"],
        }
        if "shm" in spec:
            group_block["shm_gi"] = spec["shm"]
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
    final_priority = priority if priority is not None else config.job_priority
    if final_priority is not None:
        body["task_priority"] = final_priority
    return body


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def _format_ts(raw) -> str:
    """Format a millis-since-epoch string/int into a short local time."""
    import datetime as _dt

    if raw in (None, "", 0, "0"):
        return "-"
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        return str(raw)[:20]
    try:
        return _dt.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return str(raw)[:20]


@click.command("events")
@click.argument("name")
@click.option(
    "--tail",
    type=int,
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
    default=None,
    help="Filter by event type (Normal / Warning).",
)
@pass_context
def events_ray(
    ctx: Context,
    name: str,
    tail: Optional[int],
    reason: Optional[str],
    type_filter: Optional[str],
) -> None:
    """Show events for a Ray (弹性计算) job.

    \b
    Critical for diagnosing stuck PENDING jobs — the `FailedScheduling`
    events spell out exactly why the scheduler can't place a pod
    (insufficient CPU / GPU, node affinity mismatch, taint, etc.).

    \b
    Examples:
        inspire ray events <ray-name>
        inspire ray events <ray-name> --reason FailedScheduling
        inspire ray events <ray-name> --type Warning --tail 10
        inspire --json ray events <ray-name>
    """
    try:
        ray_job_id = _resolve_ray_name(ctx, name)
        session = get_web_session()
        events = browser_api_module.list_ray_job_events(ray_job_id, session=session)

        if reason:
            events = [e for e in events if (e.get("reason") or "") == reason]
        if type_filter:
            events = [e for e in events if (e.get("type") or "") == type_filter]
        if tail is not None and tail > 0:
            events = events[-tail:]

        if ctx.json_output:
            click.echo(json_formatter.format_json({"events": events, "total": len(events)}))
            return

        if not events:
            click.echo("No Ray job events found.")
            return

        for e in events:
            ts = _format_ts(e.get("last_timestamp") or e.get("first_timestamp"))
            etype = (e.get("type") or "").ljust(7)
            reason_str = (e.get("reason") or "").ljust(28)
            msg = e.get("message") or ""
            count = e.get("count")
            tag = f" (×{count})" if count and int(count) > 1 else ""
            click.echo(f"{ts}  {etype} {reason_str} {msg}{tag}")

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


# ---------------------------------------------------------------------------
# instances
# ---------------------------------------------------------------------------


@click.command("instances")
@click.argument("name")
@pass_context
def instances_ray(ctx: Context, name: str) -> None:
    """List pod-level instances (head + workers) for a Ray job.

    \b
    Shows each pod's status — "pending" means the scheduler has the pod
    but hasn't bound it to a node yet; "running" means it's up. If the
    Ray job as a whole is PENDING but some pods are already running, it's
    usually a head-vs-worker ordering issue; if all pods are "pending",
    check `inspire ray events <name>` for the scheduling reason.
    """
    try:
        ray_job_id = _resolve_ray_name(ctx, name)
        session = get_web_session()
        instances = browser_api_module.list_ray_job_instances(ray_job_id, session=session)

        if ctx.json_output:
            click.echo(json_formatter.format_json({"instances": instances, "total": len(instances)}))
            return

        if not instances:
            click.echo("No Ray pod instances found (job may not have been scheduled yet).")
            return

        id_w = max(len("Instance"), *(len(i.get("instance_id", "")) for i in instances))
        click.echo(f"{'Type':<8} {'Group':<12} {'Status':<10} {'Instance':<{id_w}}  CPU/GPU/Mem")
        click.echo("-" * (id_w + 50))
        for inst in instances:
            itype = (inst.get("instance_type") or "").ljust(8)
            group = (inst.get("worker_group_name") or "").ljust(12)
            status = (inst.get("status") or "").ljust(10)
            iid = (inst.get("instance_id") or "").ljust(id_w)
            cpu = inst.get("cpu_count") or 0
            gpu = inst.get("gpu_count") or 0
            mem = inst.get("memory_size") or 0
            click.echo(f"{itype} {group} {status} {iid}  {cpu}C/{gpu}G/{mem}GiB")

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


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
def delete_ray(ctx: Context, name: str, yes: bool, pick: Optional[int]) -> None:
    """Permanently delete a Ray (弹性计算) job record.

    \b
    The entry disappears from the web UI. This cannot be undone; if the
    job is still running, `stop` it first so the scheduler releases
    reserved capacity cleanly.
    """
    if not yes and not ctx.json_output:
        click.confirm(
            f"Permanently delete Ray job '{name}'? This cannot be undone.",
            abort=True,
        )

    try:
        ray_job_id = _resolve_ray_name(ctx, name, pick=pick)
        session = get_web_session()
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
