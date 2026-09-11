"""`inspire serving` subcommands."""

from __future__ import annotations

import sys
import logging
import re
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
    reject_id_at_boundary,
    remember_resource_identity,
    resolve_by_name,
    run_with_stale_handle_retry,
)
from inspire.cli.utils.project_resolver import resolve_project_id as resolve_project_id_by_name
from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.task_priority import (
    TaskPriorityError,
    resolve_workspace_task_priority,
    task_priority_option,
)
from inspire.config import Config, ConfigError
from inspire.config.workspaces import (
    resolve_workspace_operation_scope,
    resolve_workspace_query_scope,
    select_workspace_id,
    workspace_label,
    workspace_name_map,
)
from inspire.platform.web.pty_socket import JobShellError
from inspire.cli.utils.job_shell import open_job_shell
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session
from inspire.services.serving.serving_instances import (
    ServingInstanceSelectionError,
    select_serving_instance_views,
    serving_instance_views,
)
from inspire.services.serving.serving_output import (
    public_configs,
    public_operation,
    public_serving,
    public_serving_list_item,
    sanitize_public_data,
    sanitize_public_text,
)
from inspire.services.serving.serving_submission import created_serving_id as _created_serving_id
from inspire.services.serving.serving_submission import resolve_image_for_create as _resolve_image_for_create
from inspire.services.serving.serving_submission import build_resource_spec_price as _build_resource_spec_price
from inspire.services.serving.serving_views import serving_resource_label as _serving_resource_label
from inspire.services.serving.serving_views import _public_serving_instance_text as _public_serving_instance_text
from inspire.services.serving.serving_views import _serving_instance_rank as _serving_instance_rank
from inspire.services.serving.serving_views import _serving_instance_resource as _serving_instance_resource
from inspire.services.serving.serving_views import public_serving_instances as _public_serving_instances
from inspire.services.serving.serving_views import public_serving_version as _public_serving_version
from inspire.services.serving.serving_views import _scale_replica_count as _scale_replica_count
from inspire.services.serving.serving_views import public_scale_history_entry as _public_scale_history_entry
from inspire.services.serving.serving_events import serving_events as _serving_events






_CUSTOM_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
logger = logging.getLogger(__name__)


def _resolve_serving_name(
    ctx: Context,
    name: str,
    *,
    workspace_id: Optional[str] = None,
    pick: Optional[int] = None,
    require_live: bool = False,
) -> str:
    """Resolve a serving name to its platform id (``sv-<uuid>``).

    Scope: ``my_serving=True`` (default) × explicit workspace, full page.
    """
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="serving",
        list_command="inspire serving list",
    )

    session = get_web_session()

    def _lister():
        items, _ = browser_api_module.list_servings(
            workspace_id=workspace_id,
            session=session,
            keyword=name,
            page_size=100,
        )
        return [
            {
                "name": s.name,
                "id": s.inference_serving_id,
                "status": s.status,
                "workspace_id": s.workspace_id,
                "created_at": s.created_at,
            }
            for s in items
        ]

    return resolve_by_name(
        ctx,
        name=name,
        resource_type="serving",
        list_candidates=_lister,
        pick_index=pick,
        session=session,
        workspace_id=str(workspace_id or ""),
        owner_scope="self",
        require_live=require_live,
        list_command="inspire serving list --workspace <workspace>",
    )


def _resolve_workspace_id(workspace: Optional[str], *, session=None) -> Optional[str]:
    if workspace is None:
        return None
    return select_workspace_id(explicit_workspace_name=workspace, session=session)


def _run_readonly_serving_operation(
    ctx: Context,
    *,
    name: str,
    workspace_id: Optional[str],
    session,
    pick: Optional[int],
    operation,
):
    """Run a read-only serving operation and recover one stale cache hit."""

    def _resolve(require_live: bool) -> str:
        return _resolve_serving_name(
            ctx,
            name,
            workspace_id=workspace_id,
            pick=pick,
            require_live=require_live,
        )

    return run_with_stale_handle_retry(
        name=name,
        resolve_cached=lambda: _resolve(False),
        resolve_live=lambda _name: _resolve(True),
        operation=lambda serving_id: operation(serving_id, session),
        invalidate=lambda serving_id: forget_resource_identity(
            session=session,
            resource_type="serving",
            resource_id=serving_id,
            name=name,
            workspace_id=str(workspace_id or ""),
            owner_scope="self",
        ),
    )




def _validate_custom_domain(_ctx: click.Context, _param: click.Parameter, value: Optional[str]) -> Optional[str]:
    from inspire.services.serving.serving_submission import validate_custom_domain
    try:
        return validate_custom_domain(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc


def _resolve_project_id(
    *,
    ctx: Context,
    workspace_id: Optional[str],
    session,
    config: Config,
    requested: Optional[str],
) -> Optional[str]:
    if not requested:
        return None
    requested = reject_id_at_boundary(
        ctx,
        requested,
        resource_type="project",
        list_command="inspire project list",
    )
    data = browser_api_module.list_serving_user_project(
        workspace_id=workspace_id, session=session
    )
    projects = data.get("projects") or []
    return resolve_project_id_by_name(
        config,
        requested,
        (item for item in projects if isinstance(item, dict)),
        name_getter=lambda item: str(
            item.get("project_name") or item.get("name") or ""
        ),
        id_getter=lambda item: str(item.get("project_id") or item.get("id") or ""),
    )






def _resolve_image_id(raw: str, *, session, workspace_id: str) -> str:
    image_id, _display = _resolve_image_for_create(
        raw, session=session, workspace_id=workspace_id
    )
    return image_id






def _resolve_model_for_create(*, name, workspace_id, project_id, user_id, session, ctx):
    from inspire.services.serving.serving_submission import resolve_model_for_create
    return resolve_model_for_create(
        name=name, workspace_id=workspace_id, project_id=project_id,
        user_id=user_id, session=session,
        resolve=lambda candidates: resolve_by_name(
            ctx, name=name, resource_type="model", list_candidates=lambda: candidates,
            session=session, workspace_id=str(workspace_id or ""), owner_scope="self",
        ),
    )




def _format_list_rows(rows: list[dict[str, str]], total: int) -> str:
    """Render public rows, preserving validated endpoints as usable URLs."""
    del total
    if not rows:
        return "No inference servings found."
    columns = [("name", "Name"), ("status", "Status")]
    columns.extend(
        (key, label)
        for key, label in (
            ("model", "Model"),
            ("endpoint", "Endpoint"),
            ("replicas", "Replicas"),
            ("project", "Project"),
            ("workspace", "Workspace"),
            ("updated_at", "Updated"),
        )
        if any(row.get(key) not in (None, "", "-") for row in rows)
    )
    table_rows = [
        tuple(
            (row.get(key) or "-") if key == "endpoint" else scrub_raw_ids(row.get(key) or "-")
            for key, _label in columns
        )
        for row in rows
    ]
    # public_serving already validated endpoint origins. Scrubbing embedded
    # handles or clipping these cells would turn them into unusable URLs.
    widths = [
        column_width(
            label,
            [row[index] for row in table_rows],
            max_width=None if key == "endpoint" else 48,
            scrub=False,
        )
        for index, (key, label) in enumerate(columns)
    ]
    rendered = render_table(
        tuple(label for _key, label in columns),
        table_rows,
        widths,
        line_char="─",
        scrub=False,
    )
    return "\n".join(rendered)










def _format_serving_instances(instances: list[dict[str, Any]]) -> str:
    """Render projected serving instances as a compact table."""
    if not instances:
        return "No serving instances found."

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




def _format_serving_versions(versions: list[dict[str, Any]]) -> str:
    if not versions:
        return "No serving versions found."
    columns = [("version", "Version")]
    columns.extend(
        (key, label)
        for key, label in (
            ("status", "Status"),
            ("model", "Model"),
            ("replicas", "Replicas"),
            ("resource", "Resource"),
            ("created_at", "Created"),
        )
        if any(item.get(key) not in (None, "") for item in versions)
    )
    table_rows = [
        tuple(str(item.get(key, "-") or "-") for key, _label in columns)
        for item in versions
    ]
    widths = [
        column_width(label, [row[index] for row in table_rows], max_width=48)
        for index, (_key, label) in enumerate(columns)
    ]
    rendered = render_table(
        tuple(label for _key, label in columns),
        table_rows,
        widths,
        line_char="─",
    )
    return "\n".join(rendered)






def _format_scale_history(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "No serving scale history found."

    def _replicas(entry: dict[str, Any]) -> str:
        before = entry.get("replicas_from")
        after = entry.get("replicas_to")
        if before is None and after is None:
            return "-"
        if before is None:
            return str(after)
        if after is None:
            return str(before)
        return f"{before} -> {after}"

    rendered_rows = [{**entry, "replicas": _replicas(entry)} for entry in entries]
    columns = [("created_at", "Created"), ("replicas", "Replicas")]
    if any(row.get("status") not in (None, "") for row in rendered_rows):
        columns.append(("status", "Status"))
    table_rows = [
        tuple(str(row.get(key, "-") or "-") for key, _label in columns)
        for row in rendered_rows
    ]
    widths = [
        column_width(label, [row[index] for row in table_rows], max_width=48)
        for index, (_key, label) in enumerate(columns)
    ]
    rendered = render_table(
        tuple(label for _key, label in columns),
        table_rows,
        widths,
        line_char="─",
    )
    return "\n".join(rendered)


def _config_label(item: dict[str, Any], index: int) -> str:
    name = (
        item.get("name")
        or item.get("config_name")
        or item.get("image_name")
        or item.get("model_name")
        or item.get("resource_name")
        or f"config {index}"
    )
    bits = []
    for key in ("gpu_type", "gpu_count", "cpu_count", "memory_size_gib", "replicas"):
        value = item.get(key)
        if value not in (None, ""):
            bits.append(f"{key.replace('_', ' ')}={value}")
    suffix = f"  ({', '.join(bits)})" if bits else ""
    return scrub_raw_ids(f"{name}{suffix}")


def _format_auto_stop(rule: str) -> str:
    if not rule:
        return "-"
    try:
        import json

        parsed = json.loads(rule)
    except Exception:
        return "-"
    conds = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if {"crit", "thresh", "hrs"}.issubset(node.keys()):
            conds.append(
                f"{node.get('crit')}<{node.get('thresh')}% for {node.get('hrs')}h"
            )
        for child in node.get("conds") or []:
            walk(child)

    walk(parsed)
    return ", ".join(conds) if conds else "-"


def _format_configs(data: dict[str, Any]) -> str:
    configs = data.get("configs") if isinstance(data, dict) else None
    if configs is None and isinstance(data, dict) and isinstance(data.get("items"), list):
        configs = {
            "items": data.get("items"),
            "enable_auto_stop": data.get("auto_stop"),
        }
    if not configs:
        return "No inference-serving configs returned (workspace may be empty or not authorized)."
    items: list[Any]
    if isinstance(configs, list):
        items = configs
        enable_auto_stop = None
    elif isinstance(configs, dict):
        raw_items = configs.get("items")
        items = raw_items if isinstance(raw_items, list) else []
        enable_auto_stop = configs.get("enable_auto_stop")
    else:
        return f"{len(configs) if isinstance(configs, dict) else 1} config section(s) available."
    if not items:
        return "No inference-serving config items returned."
    lines: list[str] = []
    if enable_auto_stop is not None:
        lines.append(f"auto-stop={'enabled' if enable_auto_stop else 'disabled'}")
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            lines.append(f"config-{i}")
            continue
        workspace = scrub_raw_ids(item.get("workspace") or "")
        gpu_min = item.get("gpu_count_min")
        gpu_max = item.get("gpu_count_max")
        bits = []
        if gpu_min is not None or gpu_max is not None:
            bits.append(f"gpu={gpu_min or '?'}-{gpu_max or '?'}")
        rule = _format_auto_stop(
            str(item.get("auto_stop_ruleset") or item.get("auto_stop_rules") or "")
        )
        if rule != "-":
            bits.append(f"auto_stop={rule}")
        if "auto_stop" in item:
            bits.append(f"auto-stop={'enabled' if item.get('auto_stop') else 'disabled'}")
        label = ", ".join(bits) if bits else _config_label(item, i)
        lines.append(f"{workspace}: {label}" if workspace else label)
    return "\n".join(lines)


@click.command("list")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME|all",
    help="Workspace name or 'all'.",
)
@click.option("--project", default=None, metavar="NAME", help="Project name filter")
@click.option(
    "--status",
    "-s",
    "status_filter",
    default=None,
    metavar="STATUS",
    help="Serving status filter",
)
@click.option(
    "--keyword",
    default=None,
    metavar="KEYWORD",
    help="Server-side name/model search",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum servings to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every serving.")
@pass_context
def list_serving(
    ctx: Context,
    workspace: Optional[str],
    project: Optional[str],
    status_filter: Optional[str],
    keyword: Optional[str],
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List the current user's inference servings.

    Status in JSON and human output is scrubbed, then uppercased; blank is UNKNOWN.

    \b
    Examples:
        inspire serving list --workspace 分布式训练空间 --project <project>
        inspire serving list --workspace 分布式训练空间 --keyword qwen --status RUNNING
        inspire serving list --workspace all --keyword qwen
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
        workspace_names = workspace_name_map(session)
        items: list[tuple[Any, str]] = []
        total = 0
        matched_project_scope = project is None
        for workspace_id in workspace_ids:
            try:
                project_id = _resolve_project_id(
                    ctx=ctx,
                    workspace_id=workspace_id,
                    session=session,
                    config=config,
                    requested=project,
                )
            except ConfigError as e:
                if all_workspaces and str(e).startswith("Unknown project name "):
                    continue
                raise
            matched_project_scope = True
            workspace_items, workspace_total = browser_api_module.list_servings(
                workspace_id=workspace_id,
                keyword=keyword,
                project_ids=[project_id] if project_id else None,
                statuses=[status_filter] if status_filter else None,
                page=1,
                page_size=request_limit,
                session=session,
            )
            if show_all and workspace_total > len(workspace_items):
                workspace_items, expanded_total = browser_api_module.list_servings(
                    workspace_id=workspace_id,
                    keyword=keyword,
                    project_ids=[project_id] if project_id else None,
                    statuses=[status_filter] if status_filter else None,
                    page=1,
                    page_size=max(workspace_total, len(workspace_items), 1),
                    session=session,
                )
                workspace_total = max(
                    workspace_total,
                    expanded_total,
                    len(workspace_items),
                )
            workspace_name = workspace_names.get(workspace_id) or (
                "(workspace name unavailable)"
            )
            items.extend((item, workspace_name) for item in workspace_items)
            total += max(workspace_total, len(workspace_items))
        if not matched_project_scope:
            raise ConfigError(
                f"Unknown project name {project!r} in the requested workspaces."
            )
        if all_workspaces:
            items.sort(
                key=lambda pair: str(
                    getattr(pair[0], "updated_at", "")
                    or getattr(pair[0], "created_at", "")
                    or ""
                ),
                reverse=True,
            )
        page = bound_collection(items, limit=effective_limit, total=total)

        if ctx.json_output:
            public_items = [
                public_serving_list_item(
                    serving,
                    fallback_workspace=workspace_name,
                )
                for serving, workspace_name in page.items
            ]
            click.echo(
                json_formatter.format_json(
                    {
                        **page.metadata(),
                        "items": public_items,
                    },
                    preserve_raw={"endpoint"}
                )
            )
            return

        rows = []
        for serving, workspace_name in page.items:
            projected = public_serving(
                serving,
                fallback_name=str(getattr(serving, "name", "") or ""),
            )
            replicas = projected.get("replicas")
            nodes_per_replica = projected.get("nodes_per_replica")
            rows.append(
                {
                    "name": str(projected.get("name") or "-"),
                    "status": str(projected.get("status") or "-"),
                    "model": str(projected.get("model") or "-"),
                    "replicas": (
                        f"{replicas}x{nodes_per_replica}"
                        if nodes_per_replica not in (None, "")
                        else str(replicas or "-")
                    ),
                    "endpoint": str(projected.get("endpoint") or "-"),
                    "project": str(projected.get("project") or "-"),
                    "workspace": (
                        scrub_raw_ids(workspace_name) if all_workspaces else "-"
                    ),
                    "updated_at": str(
                        projected.get("updated_at")
                        or projected.get("created_at")
                        or "-"
                    ),
                }
            )
        click.echo(_format_list_rows(rows, total=int(total) if total is not None else len(rows)))
        notice = truncation_notice(page, full_option="--all")
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


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
def status_serving(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    pick: Optional[int],
) -> None:
    """Show detail for one inference serving by name.

    Status in JSON and human output is scrubbed, then uppercased; blank is UNKNOWN.

    Detail includes status, project, model, image, resource, startup command,
    port, replicas, endpoint, and timestamps when the platform returns them.
    Use serving api for authentication and invocation examples.
    """
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="serving",
        list_command="inspire serving list --workspace <workspace>",
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        workspace_id = _resolve_workspace_id(workspace)
        session = get_web_session()
        inference_serving_id, data = run_with_stale_handle_retry(
            name=name,
            resolve_cached=lambda: _resolve_serving_name(
                ctx,
                name,
                workspace_id=workspace_id,
                pick=pick,
            ),
            resolve_live=lambda live_name: _resolve_serving_name(
                ctx,
                live_name,
                workspace_id=workspace_id,
                pick=pick,
                require_live=True,
            ),
            operation=lambda serving_id: (
                serving_id,
                browser_api_module.get_serving_detail(
                    inference_serving_id=serving_id,
                    session=session,
                ),
            ),
            invalidate=lambda serving_id: forget_resource_identity(
                session=session,
                resource_type="serving",
                resource_id=serving_id,
                name=name,
                workspace_id=str(workspace_id or ""),
                owner_scope="self",
            ),
        )
        remember_resource_identity(
            session=session,
            resource_type="serving",
            resource_id=inference_serving_id,
            name=name,
            workspace_id=str(workspace_id or ""),
            owner_scope="self",
            status=str(data.get("status") or ""),
            created_at=str(data.get("created_at") or ""),
        )

        if ctx.json_output:
            detail = public_serving(data, fallback_name=name)
            resource_label = _serving_resource_label(data)
            if resource_label:
                detail["resource"] = resource_label
            click.echo(json_formatter.format_json(detail, preserve_raw={"endpoint"}))
            return

        detail = public_serving(data, fallback_name=name)
        lines = [
            f"Name: {detail.get('name') or name}",
            f"Status: {detail.get('status') or 'N/A'}",
        ]
        for key, label in (
            ("endpoint", "Endpoint"),
            ("type", "Type"),
            ("project", "Project"),
            ("workspace", "Workspace"),
            ("compute_group", "Compute Group"),
            ("created_by", "Created By"),
            ("replicas", "Replicas"),
            ("nodes_per_replica", "Nodes/rep"),
            ("priority", "Priority"),
            ("image", "Image"),
            ("model", "Model"),
            ("resource", "Resource"),
            ("nodes", "Nodes"),
            ("command", "Command"),
            ("port", "Port"),
            ("created_at", "Created"),
            ("updated_at", "Updated"),
        ):
            value = detail.get(key)
            if value in (None, ""):
                continue
            lines.append(
                f"{label}: {', '.join(value) if isinstance(value, list) else value}"
            )
        click.echo("\n".join(lines))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("start")
@click.argument("name", metavar="NAME")
@click.option("--workspace", metavar="NAME", required=True, help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def start_serving(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    pick: Optional[int],
) -> None:
    """Start an inference serving by name."""
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="serving",
        list_command="inspire serving list --workspace <workspace>",
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(workspace)
        inference_serving_id = _resolve_serving_name(
            ctx,
            name,
            workspace_id=workspace_id,
            pick=pick,
            require_live=True,
        )
        browser_api_module.start_serving(
            inference_serving_id=inference_serving_id,
            session=session,
        )

        if ctx.json_output:
            click.echo(json_formatter.format_json(public_operation(name, "started")))
            return

        click.echo(human_formatter.format_mutation_success("Serving", "started", name))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
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
def stop_serving(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    pick: Optional[int],
) -> None:
    """Stop an inference serving (pass the serving name)."""
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="serving",
        list_command="inspire serving list --workspace <workspace>",
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(workspace)
        inference_serving_id = _resolve_serving_name(
            ctx,
            name,
            workspace_id=workspace_id,
            pick=pick,
            require_live=True,
        )
        browser_api_module.stop_serving(
            inference_serving_id=inference_serving_id,
            session=session,
        )

        if ctx.json_output:
            click.echo(json_formatter.format_json(public_operation(name, "stopped")))
            return

        click.echo(human_formatter.format_mutation_success("Serving", "stopped", name))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("scale")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--replicas",
    type=click.IntRange(0),
    required=True,
    help="Target replica count for the deployment.",
)
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def scale_serving(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    replicas: int,
    pick: Optional[int],
) -> None:
    """Change how many replicas an inference serving runs.

    Scaling reuses the deployment's existing image, command, port and resource
    spec — only the replica count moves. Each replica costs the serving's full
    quota, so check `inspire serving quota --workspace <workspace>` before
    scaling up. Watch the result with `inspire serving instances <name>
    --workspace <workspace>`.
    """
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="serving",
        list_command="inspire serving list --workspace <workspace>",
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(workspace)
        inference_serving_id = _resolve_serving_name(
            ctx,
            name,
            workspace_id=workspace_id,
            pick=pick,
            require_live=True,
        )
        browser_api_module.scale_serving(
            inference_serving_id,
            replica=replicas,
            session=session,
        )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    public_operation(name, "scaled", replicas=replicas)
                )
            )
            return
        click.echo(
            human_formatter.format_mutation_success(
                "Serving", f"scaled to {replicas} replica(s)", name
            )
        )

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("versions")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
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
    help="Maximum versions to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every version.")
@pass_context
def versions_serving(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    pick: Optional[int],
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List a serving's deployment history.

    Each row is one configuration the deployment has run under. The version
    number is what `inspire serving rollback --version` takes.
    """
    try:
        output_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="serving",
        list_command="inspire serving list --workspace <workspace>",
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(workspace)
        items, total = _run_readonly_serving_operation(
            ctx,
            name=name,
            workspace_id=workspace_id,
            session=session,
            pick=pick,
            operation=lambda serving_id, live_session: (
                browser_api_module.list_serving_versions(
                    serving_id,
                    session=live_session,
                )
            ),
        )
        projected = [_public_serving_version(item) for item in items]
        page = bound_collection(projected, limit=output_limit, total=total)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "name": scrub_raw_ids(name),
                        "items": page.items,
                        **page.metadata(),
                    }
                )
            )
            return

        click.echo(_format_serving_versions(page.items))
        notice = truncation_notice(page, full_option="--all")
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("scale-history")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
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
    help="Maximum scale events to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every scale event.")
@pass_context
def scale_history_serving(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    pick: Optional[int],
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List when a serving's replica count changed, and to what.

    This is the first thing to check when request latency or throughput moved
    without a redeploy: a replica count that dropped, or an autoscale that
    never landed, shows up here and nowhere in `versions`. Pair it with
    `inspire serving api-metrics <name>` to line the change up against the
    traffic it explains.
    """
    try:
        output_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    request_limit = (
        output_limit if output_limit is not None else DEFAULT_COLLECTION_LIMIT
    )
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="serving",
        list_command="inspire serving list --workspace <workspace>",
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(workspace)

        def _fetch(serving_id: str, live_session):  # noqa: ANN001
            items, total = browser_api_module.list_serving_scale_history(
                serving_id,
                page=1,
                page_size=request_limit,
                session=live_session,
            )
            if show_all and total > len(items):
                items, expanded_total = browser_api_module.list_serving_scale_history(
                    serving_id,
                    page=1,
                    page_size=max(total, len(items), 1),
                    session=live_session,
                )
                total = max(total, expanded_total, len(items))
            return items, total

        items, total = _run_readonly_serving_operation(
            ctx,
            name=name,
            workspace_id=workspace_id,
            session=session,
            pick=pick,
            operation=_fetch,
        )
        projected = [_public_scale_history_entry(item) for item in items]
        page = bound_collection(projected, limit=output_limit, total=total)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "name": scrub_raw_ids(name),
                        "items": page.items,
                        **page.metadata(),
                    }
                )
            )
            return

        click.echo(_format_scale_history(page.items))
        notice = truncation_notice(page, full_option="--all")
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("rollback")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--version",
    type=click.IntRange(1),
    required=True,
    help="Version to roll back to, from `inspire serving versions <name>`.",
)
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
def rollback_serving(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    version: int,
    yes: bool,
    pick: Optional[int],
) -> None:
    """Redeploy an inference serving under an earlier version's configuration.

    Pick the target with `inspire serving versions <name> --workspace
    <workspace>`. The running replicas are replaced, so in-flight requests are
    interrupted the same way a restart interrupts them.
    """
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="serving",
        list_command="inspire serving list --workspace <workspace>",
    )
    require_confirmation(
        ctx,
        yes=yes,
        prompt=(
            f"Roll inference serving '{scrub_raw_ids(name)}' back to version "
            f"{version}? Running replicas are replaced."
        ),
        message="Inference serving rollback requires confirmation.",
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(workspace)
        inference_serving_id = _resolve_serving_name(
            ctx,
            name,
            workspace_id=workspace_id,
            pick=pick,
            require_live=True,
        )
        browser_api_module.rollback_serving(
            inference_serving_id,
            version=version,
            session=session,
        )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    public_operation(name, "rolled back", version=version)
                )
            )
            return
        click.echo(
            human_formatter.format_mutation_success(
                "Serving", f"rolled back to version {version}", name
            )
        )

    except click.Abort:
        raise
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


# One page covers any deployment: a serving never has enough replicas to page.
_INSTANCE_EVENT_FETCH_SIZE = 200




@click.command("events")
@click.argument("name", metavar="NAME")
@click.option("--workspace", metavar="NAME", required=True, help="Workspace name.")
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
    "reason_filter",
    default=None,
    metavar="REASON",
    help="Filter events whose reason contains this substring.",
)
@click.option(
    "--instance",
    "instance_selectors",
    multiple=True,
    metavar="RANK",
    help=(
        "Narrow to one replica, named by the Name column of `inspire serving "
        "instances` — `rank=0`, or just `0`. Repeat for several. Default: "
        "deployment events plus every replica."
    ),
)
@click.option(
    "--workload-level",
    "workload_level",
    is_flag=True,
    help=(
        "Only the controller's own events about the deployment as a whole. "
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
        "Follow and print new events. Runs until interrupted; it never exits on its own, "
        "not even once the serving reaches a terminal state."
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
def events_serving(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    pick: Optional[int],
    reason_filter: Optional[str],
    type_filter: Optional[str],
    instance_selectors: tuple[str, ...],
    workload_level: bool,
    tail: int,
    follow: bool,
    interval: int,
) -> None:
    """Show lifecycle and scheduling events for an inference serving.

    Deployment events (`CreatingRevision` / `GroupsProgressing` / `Pending`)
    and every replica's pod events (`Scheduled` / `Pulled` / `Started`) are
    disjoint sets, and the default merges both into one timeline with an
    `Instance` column. Use ``--instance`` to narrow to one replica, or
    ``--workload-level`` to keep only the controller's half.

    \b
    Examples:
      inspire serving events my-serving --workspace CPU资源空间
      inspire serving events my-serving --workspace CPU资源空间 --instance rank=0
      inspire serving events my-serving --workspace CPU资源空间 --workload-level
      inspire --json serving events my-serving --workspace CPU资源空间
    """
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="serving",
        list_command="inspire serving list --workspace <workspace>",
    )
    if workload_level and instance_selectors:
        _handle_error(
            ctx,
            "InvalidUsage",
            "--workload-level and --instance cannot be used together.",
            EXIT_VALIDATION_ERROR,
        )
        return
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(workspace)

        def _fetch_events() -> list[dict[str, Any]]:
            # An unknown `--instance` is a usage error; the shared runner would
            # otherwise repackage it as "could not fetch events".
            try:
                return _run_readonly_serving_operation(
                    ctx,
                    name=name,
                    workspace_id=workspace_id,
                    session=session,
                    pick=pick,
                    operation=lambda serving_id, live_session: _serving_events(
                        serving_id,
                        session=live_session,
                        selectors=instance_selectors,
                        workload_level=workload_level,
                    ),
                )
            except ServingInstanceSelectionError as e:
                _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
                return []

        run_events_command(
            ctx,
            fetch=_fetch_events,
            type_filter=type_filter,
            reason_filter=reason_filter,
            tail=tail,
            follow=follow,
            interval=interval,
        )

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("instances")
@click.argument("name", metavar="NAME")
@click.option("--workspace", metavar="NAME", required=True, help="Workspace name.")
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
@click.option("--all", "show_all", is_flag=True, help="Show every instance.")
@pass_context
def instances_serving(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    pick: Optional[int],
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List runtime instances for an inference serving by name."""
    try:
        output_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    request_limit = (
        output_limit if output_limit is not None else DEFAULT_COLLECTION_LIMIT
    )
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="serving",
        list_command="inspire serving list --workspace <workspace>",
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(workspace)

        def _fetch(serving_id: str, live_session):
            items, total = browser_api_module.list_serving_instances(
                serving_id,
                page=1,
                page_size=request_limit,
                session=live_session,
            )
            if show_all and total > len(items):
                items, expanded_total = browser_api_module.list_serving_instances(
                    serving_id,
                    page=1,
                    page_size=max(total, len(items), 1),
                    session=live_session,
                )
                total = max(total, expanded_total, len(items))
            return items, total

        items, total = _run_readonly_serving_operation(
            ctx,
            name=name,
            workspace_id=workspace_id,
            session=session,
            pick=pick,
            operation=_fetch,
        )
        projected = _public_serving_instances(items)
        page = bound_collection(projected, limit=output_limit, total=total)

        if ctx.json_output:
            payload: dict[str, Any] = {
                "name": scrub_raw_ids(name),
                "items": page.items,
                **page.metadata(),
            }
            click.echo(
                json_formatter.format_json(payload)
            )
            return

        click.echo(_format_serving_instances(page.items))
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


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
def delete_serving(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    yes: bool,
    pick: Optional[int],
) -> None:
    """Delete an inference serving entry (pass the serving name)."""
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="serving",
        list_command="inspire serving list --workspace <workspace>",
    )
    require_confirmation(
        ctx,
        yes=yes,
        prompt=(
            f"Permanently delete inference serving '{scrub_raw_ids(name)}'? "
            "This cannot be undone."
        ),
        message="Inference serving deletion requires confirmation.",
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(workspace)
        inference_serving_id = _resolve_serving_name(
            ctx,
            name,
            workspace_id=workspace_id,
            pick=pick,
            require_live=True,
        )
        browser_api_module.delete_serving(
            inference_serving_id=inference_serving_id,
            session=session,
        )
        forget_resource_identity(
            session=session,
            resource_type="serving",
            resource_id=inference_serving_id,
            name=name,
            workspace_id=str(workspace_id or ""),
            owner_scope="self",
        )

        if ctx.json_output:
            click.echo(json_formatter.format_json(public_operation(name, "deleted")))
            return

        click.echo(human_formatter.format_mutation_success("Serving", "deleted", name))

    except click.Abort:
        raise
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("configs")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME",
    help="Workspace name.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum choices to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every available choice.")
@pass_context
def configs_serving(
    ctx: Context,
    workspace: Optional[str],
    limit: int | None,
    show_all: bool,
) -> None:
    """Show available inference-serving choices by workspace.

    Use this before `serving create` to inspect deployment settings exposed
    by the platform. Use `serving quota --workspace <name>` to choose the
    concrete `--quota gpu,cpu,mem` triple.
    """
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = resolve_workspace_operation_scope(
            workspace=workspace,
            session=session,
        )
        data = browser_api_module.get_serving_configs(
            workspace_id=workspace_id,
            session=session,
        )
        public_data = public_configs(data)
        items = [
            {
                **item,
                **(
                    {"auto_stop": public_data["auto_stop"]}
                    if "auto_stop" in public_data
                    else {}
                ),
            }
            for item in public_data.get("items", [])
        ]
        page = bound_collection(items, limit=effective_limit)
        output = {
            "items": page.items,
            **page.metadata(),
        }

        if ctx.json_output:
            click.echo(json_formatter.format_json(output))
            return

        click.echo(_format_configs(output))
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("create")
@click.option("--name", "-n", required=True, metavar="NAME", help="Serving name")
@click.option(
    "--model",
    "model_name",
    required=True,
    metavar="NAME",
    help="Registered model name; scans up to 100 pages of 100 models. "
    "Incomplete lookup fails before creation; use a more specific name or workspace.",
)
@click.option(
    "--model-version",
    type=click.IntRange(1),
    default=None,
    help="Model version (default: latest version from model list)",
)
@click.option("--command", "-c", required=True, help="Serving startup command")
@click.option(
    "--port",
    type=click.IntRange(1, 65535),
    required=True,
    help="Service port in the container",
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
    metavar="NAME",
    help=(
        "Full compute group name copied from the same quota row as --quota."
    ),
)
@click.option(
    "--quota",
    "-q",
    required=True,
    metavar="SPEC",
    help="Serving resource as gpu,cpu,mem.",
)
@click.option(
    "--image",
    "-i",
    required=True,
    metavar="NAME|URL",
    help="Visible image name or name:tag.",
)
@click.option(
    "--replicas",
    type=click.IntRange(1),
    default=1,
    show_default=True,
    help=(
        "How many replicas to serve behind the endpoint. Each one costs the full "
        "--quota, and the count can be changed later with 'inspire serving scale'."
    ),
)
@click.option(
    "--nodes-per-replica",
    type=click.IntRange(1),
    default=1,
    show_default=True,
    help=(
        "Nodes a single replica spans, for a model too large for one node. "
        "'inspire model deploy-config <model>' reports the floor for this and --quota."
    ),
)
@click.option(
    "--shm-size",
    type=click.IntRange(1),
    default=None,
    help="Shared memory size in GiB.",
)
@task_priority_option()
@click.option(
    "--custom-domain",
    default=None,
    callback=_validate_custom_domain,
    help="Optional domain prefix: lowercase letters, digits, and hyphens",
)
@click.option("--description", default="", help="Serving description")
@click.option(
    "--auto-scaling/--no-auto-scaling",
    "auto_scaling",
    default=None,
    help=(
        "Let the platform move the replica count with load "
        "(平台 弹性伸缩). Omit to leave the platform default."
    ),
)
@click.option(
    "--public-path-readonly/--no-public-path-readonly",
    default=None,
    help=(
        "Mount the project's public path read-only inside the serving container "
        "(平台 高级设置·项目Public只读挂载). Omit to leave the platform default."
    ),
)
@click.option("--dry-run", is_flag=True, default=False, help="Print the resolved plan without creating")
@pass_context
def create_serving(
    ctx: Context,
    name: str,
    model_name: str,
    model_version: Optional[int],
    workspace: Optional[str],
    project: Optional[str],
    group: Optional[str],
    quota: Optional[str],
    image: Optional[str],
    command: str,
    port: int,
    replicas: int,
    nodes_per_replica: int,
    shm_size: Optional[int],
    priority: Optional[int],
    custom_domain: Optional[str],
    description: str,
    auto_scaling: Optional[bool],
    public_path_readonly: Optional[bool],
    dry_run: bool,
) -> None:
    """Create an inference serving from a registered model.

    Pick the model with `model list/status/versions`, choose a serving spec
    with `serving quota --workspace <name>`, then submit the service with a
    visible image, startup command, and container port. Omit
    `--model-version` to use the latest version reported by the model list.

    \b
    Examples:
        inspire serving create --name qwen-demo --model qwen-demo --workspace 分布式训练空间 \\
          --project <project> --group H200-2号机房 --quota 1,18,200 \\
          --image <image> --command "python serve.py" --port 8000 --dry-run
        inspire serving metrics qwen-demo --workspace 分布式训练空间 --window 30m
    """
    try:
        from inspire.cli.utils.quota_resolver import (
            QuotaMatchError,
            QuotaParseError,
            SCHEDULE_TYPE_SERVING,
            ensure_priority_allowed,
            parse_quota,
            resolve_quota,
        )

        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()

        for field_name, value in (
            ("workspace", workspace),
            ("project", project),
            ("group", group),
            ("quota", quota),
            ("image", image),
        ):
            if not value:
                raise ConfigError(f"--{field_name} is required.")
        workspace = cast(str, workspace)
        project = cast(str, project)
        group = cast(str, group)
        image = cast(str, image)
        quota = cast(str, quota)

        workspace_id = select_workspace_id(
            explicit_workspace_name=workspace,
            session=session,
        )
        if not workspace_id:
            raise ConfigError("--workspace is required.")
        project_id = _resolve_project_id(
            ctx=ctx,
            workspace_id=workspace_id,
            session=session,
            config=config,
            requested=project,
        )
        if not project_id:
            raise ConfigError("--project is required.")
        user = browser_api_module.get_current_user(session=session)
        current_user_id = str(user.get("id") or user.get("user_id") or "").strip()
        if not current_user_id:
            raise ConfigError("Cannot determine the current user from the live web session.")

        try:
            spec = parse_quota(quota)
            resolved = resolve_quota(
                spec=spec,
                workspace_id=workspace_id,
                session=session,
                schedule_config_type=SCHEDULE_TYPE_SERVING,
                group_override=group,
            )
        except (QuotaParseError, QuotaMatchError) as exc:
            raise click.UsageError(str(exc)) from exc

        model_id, latest_version, model_label = _resolve_model_for_create(
            name=model_name,
            workspace_id=workspace_id,
            project_id=None,
            user_id=current_user_id,
            session=session,
            ctx=ctx,
        )
        final_model_version = model_version or latest_version
        if final_model_version is None:
            raise ConfigError(
                "Could not infer model version. Pass --model-version explicitly."
            )

        mirror_id, image_label = _resolve_image_for_create(
            image, session=session, workspace_id=workspace_id
        )
        resource_spec_price = _build_resource_spec_price(resolved)
        final_priority = resolve_workspace_task_priority(
            priority,
            session=session,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        try:
            ensure_priority_allowed(
                resolved, final_priority, quota_command="inspire serving quota"
            )
        except QuotaMatchError as exc:
            # Reported directly rather than raised: the outer `except Exception`
            # would file a validation failure as an APIError.
            _handle_error(ctx, "ValidationError", str(exc), EXIT_VALIDATION_ERROR)
            return
        payload = {
            "name": name,
            "logic_compute_group_id": resolved.logic_compute_group_id,
            "project_id": project_id,
            "workspace_id": workspace_id,
            "mirror_id": mirror_id,
            "command": command,
            "description": description,
            "model_id": model_id,
            "model_version": final_model_version,
            "port": port,
            "replicas": replicas,
            "node_num_per_replica": nodes_per_replica,
            "shm_gi": shm_size,
            "task_priority": final_priority,
            "resource_spec_price": resource_spec_price,
        }
        if custom_domain:
            payload["custom_domain"] = custom_domain
        # Only an explicit flag reaches the wire; the platform keeps owning the
        # default so an untouched create stays byte-for-byte what it was.
        if auto_scaling is not None:
            payload["enable_auto_scaling"] = bool(auto_scaling)
        if public_path_readonly is not None:
            payload["is_publicpath_readonly"] = bool(public_path_readonly)

        if dry_run:
            plan = sanitize_public_data(
                {
                    "dry_run": True,
                    "name": name,
                    "workspace": workspace_label(session, workspace_id, workspace),
                    "project": project,
                    "compute_group": resolved.compute_group_name,
                    "resource": {
                        "gpu": resolved.gpu_count,
                        "cpu": resolved.cpu_count,
                        "memory_gib": resolved.memory_gib,
                    },
                    "image": image_label,
                    "model": model_label,
                    "model_version": final_model_version,
                    "command": command,
                    "description": description,
                    "port": port,
                    "replicas": replicas,
                    "nodes_per_replica": nodes_per_replica,
                    "shared_memory_gib": shm_size,
                    "priority": final_priority,
                    "custom_domain": custom_domain,
                    "auto_scaling": auto_scaling,
                    "public_path_readonly": public_path_readonly,
                },
                omit_urls=True,
            )
            if ctx.json_output:
                click.echo(json_formatter.format_json(plan))
            else:
                click.echo(
                    f"Create plan: {sanitize_public_text(name, omit_urls=True)}"
                )
                click.echo(f"Project: {sanitize_public_text(project, omit_urls=True)}")
                click.echo(f"Workspace: {sanitize_public_text(workspace, omit_urls=True)}")
                click.echo(
                    "Compute: "
                    f"{sanitize_public_text(resolved.compute_group_name, omit_urls=True)}"
                )
                click.echo(f"Resource: {spec.display()}")
                click.echo(f"Image: {sanitize_public_text(image_label, omit_urls=True)}")
                click.echo(
                    f"Model: {sanitize_public_text(model_label, omit_urls=True)} "
                    f"v{final_model_version}"
                )
                click.echo(f"Command: {sanitize_public_text(command, omit_urls=True)}")
                click.echo(f"Port: {port}")
                click.echo(f"Replicas: {replicas} x {nodes_per_replica} node(s)")
                if shm_size is not None:
                    click.echo(f"Shared memory: {shm_size} GiB")
                if final_priority is not None:
                    click.echo(f"Priority: {final_priority}")
                if custom_domain:
                    click.echo(
                        f"Domain: {sanitize_public_text(custom_domain, omit_urls=True)}"
                    )
                if auto_scaling is not None:
                    click.echo(
                        "Auto scaling: enabled" if auto_scaling else "Auto scaling: disabled"
                    )
                if public_path_readonly is not None:
                    click.echo(
                        "Public path: read-only"
                        if public_path_readonly
                        else "Public path: writable"
                    )
            return

        result = browser_api_module.create_serving(
            workspace_id=workspace_id,
            project_id=project_id,
            name=name,
            logic_compute_group_id=resolved.logic_compute_group_id,
            model_id=model_id,
            model_version=final_model_version,
            mirror_id=mirror_id,
            command=command,
            port=port,
            description=description,
            replicas=replicas,
            node_num_per_replica=nodes_per_replica,
            shm_gi=shm_size,
            task_priority=final_priority,
            custom_domain=custom_domain,
            resource_spec_price=resource_spec_price,
            is_publicpath_readonly=public_path_readonly,
            enable_auto_scaling=auto_scaling,
            session=session,
        )
        serving_id = _created_serving_id(result)
        if serving_id:
            remember_resource_identity(
                session=session,
                resource_type="serving",
                resource_id=serving_id,
                name=name,
                workspace_id=workspace_id,
                owner_scope="self",
                status=str(result.get("status") or ""),
                created_at=str(result.get("created_at") or ""),
            )
        from inspire.services.serving.serving_access import serving_endpoint

        created = public_operation(name, "created")
        endpoint = serving_endpoint(result)
        if not endpoint and serving_id:
            try:
                endpoint = serving_endpoint(
                    browser_api_module.get_serving_detail(serving_id, session=session)
                )
            except Exception:
                # Creation has succeeded. A failed read must never invite a
                # duplicate create or misreport the mutation as failed.
                pass
        if endpoint:
            created["endpoint"] = endpoint
        else:
            created["hint"] = "Endpoint not available yet; use serving status or serving api later."
        if ctx.json_output:
            click.echo(json_formatter.format_json(created, preserve_raw={"endpoint"}))
            return
        click.echo(human_formatter.format_mutation_success("Serving", "created", name))
        click.echo(f"Endpoint: {endpoint}" if endpoint else created["hint"])

    except TaskPriorityError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = [
    "create_serving",
    "delete_serving",
    "list_serving",
    "rollback_serving",
    "scale_serving",
    "status_serving",
    "stop_serving",
    "versions_serving",
    "configs_serving",
]


@click.command("shell")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
@click.option(
    "--instance",
    "instance",
    default=None,
    metavar="NAME",
    help="Open this instance, as named by `inspire serving instances`.",
)
@pass_context
def shell_serving(
    ctx: Context,
    name: str,
    workspace: str,
    pick: Optional[int],
    instance: Optional[str],
) -> None:
    """Open an interactive shell inside a running serving instance.

    Needs a terminal: this attaches your stdin to a remote PTY. Leave with
    `exit`, or press Ctrl+] to drop the session without ending the shell.

    Every replica runs the same image and command, so the first running one is
    as good as any unless a specific replica is the one misbehaving; name it
    with `--instance`.

    \b
    Examples:
        inspire serving shell qwen-chat --workspace 分布式训练空间
        inspire serving shell qwen-chat --workspace 分布式训练空间 --instance rank=1
    """
    try:
        session = get_web_session()
        workspace_id = select_workspace_id(
            explicit_workspace_name=workspace, session=session
        )
        serving_id, instances = _run_readonly_serving_operation(
            ctx,
            name=name,
            workspace_id=workspace_id,
            session=session,
            pick=pick,
            operation=lambda resolved_id: (
                resolved_id,
                browser_api_module.list_serving_instances(
                    resolved_id, page=1, page_size=200, session=session
                )[0],
            ),
        )

        running = [
            row
            for row in instances
            if "running" in str(row.get("status") or "").lower()
        ]
        views = serving_instance_views(running)
        if not views:
            _handle_error(
                ctx,
                "ValidationError",
                "No running instances found for this serving.",
                EXIT_VALIDATION_ERROR,
            )
            return

        selected = (
            select_serving_instance_views(views, [instance])[0] if instance else views[0]
        )

        if not ctx.json_output:
            click.echo(
                f"Opening shell: {scrub_raw_ids(name)} / {selected.label}", err=True
            )
            click.echo("Press Ctrl-] to disconnect.", err=True)

        sys.exit(
            open_job_shell(
                job_id=serving_id,
                instance_name=selected.handle,
                session=session,
                workload="serving",
            )
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
    except JobShellError as e:
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", scrub_raw_ids(e), EXIT_VALIDATION_ERROR)
