"""`inspire serving` subcommands."""

from __future__ import annotations

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
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.collection_output import (
    DEFAULT_COLLECTION_LIMIT,
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import (
    exit_with_error as _handle_error,
    require_confirmation,
)
from inspire.cli.utils.events import DEFAULT_EVENT_TAIL, run_events_command
from inspire.cli.utils.id_resolver import (
    NAME_PICK_HELP,
    forget_resource_identity,
    looks_like_platform_id,
    reject_id_at_boundary,
    remember_resource_identity,
    resolve_by_name,
    run_with_stale_handle_retry,
)
from inspire.cli.utils.project_resolver import resolve_project_id as resolve_project_id_by_name
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.task_priority import (
    TaskPriorityError,
    resolve_workspace_task_priority,
    task_priority_option,
)
from inspire.config import Config, ConfigError
from inspire.config.workload_profiles import apply_workload_profile, profile_required_message
from inspire.config.workspaces import (
    resolve_workspace_query_scope,
    select_workspace_id,
    workspace_label,
    workspace_name_map,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session

from .public_output import (
    public_configs,
    public_operation,
    public_serving,
    public_serving_list_item,
    sanitize_public_data,
    sanitize_public_text,
)

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


def _created_serving_id(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("inference_serving_id", "serving_id", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    for key in ("inference_serving", "serving", "data", "result"):
        value = _created_serving_id(payload.get(key))
        if value:
            return value
    return ""


def _validate_custom_domain(_ctx: click.Context, _param: click.Parameter, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if not _CUSTOM_DOMAIN_RE.fullmatch(text):
        raise click.BadParameter(
            "must use lowercase letters, digits, and hyphens, and cannot start or end with a hyphen"
        )
    return text


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


def _resolve_image_for_create(raw: str, *, session) -> tuple[str, str]:
    """Resolve a visible image label to the `mirror_id` used by the web UI."""
    raw = (raw or "").strip()
    if not raw:
        raise ConfigError("Image is empty.")
    if raw.startswith(("image-", "mirror-")):
        raise ConfigError("--image takes a visible image name or name:tag.")
    target = raw.lower()
    for source in ("private", "public", "official"):
        try:
            images = browser_api_module.list_images_by_source(source=source, session=session)
        except Exception as e:  # noqa: BLE001
            logger.debug("Image lookup failed for source %s: %s", source, e)
            continue
        for img in images:
            labels = {
                str(img.url or "").strip(),
                str(img.name or "").strip(),
            }
            if img.name and img.version:
                labels.add(f"{img.name}:{img.version}")
            if target in {label.lower() for label in labels if label}:
                image_id = str(img.image_id or "").strip()
                if image_id:
                    display = f"{img.name}:{img.version}" if img.name and img.version else raw
                    return image_id, display
                break
    raise ConfigError(f"Unknown image: {raw!r}.")


def _resolve_image_id(raw: str, *, session) -> str:
    image_id, _display = _resolve_image_for_create(raw, session=session)
    return image_id


def _price_value(raw_price: dict[str, Any], nested_key: str, key: str) -> Any:
    nested = raw_price.get(nested_key)
    if isinstance(nested, dict) and nested.get(key) not in (None, ""):
        return nested.get(key)
    return raw_price.get(key)


def _build_resource_spec_price(resolved) -> dict[str, Any]:  # noqa: ANN001
    """Build the nested Browser API `resource_spec_price` payload."""
    raw_price = resolved.raw_price if isinstance(resolved.raw_price, dict) else {}
    payload = {
        "cpu_type": _price_value(raw_price, "cpu_info", "cpu_type"),
        "cpu_count": resolved.cpu_count,
        "gpu_type": _price_value(raw_price, "gpu_info", "gpu_type"),
        "gpu_count": resolved.gpu_count,
        "memory_size_gib": resolved.memory_gib,
        "logic_compute_group_id": resolved.logic_compute_group_id,
        "quota_id": resolved.quota_id,
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _resolve_model_for_create(
    *,
    name: str,
    workspace_id: Optional[str],
    project_id: Optional[str],
    user_id: str,
    session,
    ctx: Context,
) -> tuple[str, Optional[int], str]:
    items, _ = browser_api_module.list_models(
        workspace_id=workspace_id,
        keyword=name,
        project_ids=[project_id] if project_id else None,
        user_id=user_id,
        page=1,
        page_size=100,
        session=session,
    )
    candidates = [
        {
            "name": item.name,
            "id": item.model_id,
            "status": item.status,
            "created_at": item.created_at,
            "version": item.latest_version,
        }
        for item in items
    ]
    model_id = resolve_by_name(
        ctx,
        name=name,
        resource_type="model",
        list_candidates=lambda: candidates,
        session=session,
        workspace_id=str(workspace_id or ""),
        owner_scope="self",
    )
    for item in items:
        if item.model_id == model_id:
            try:
                return (
                    model_id,
                    int(item.latest_version) if item.latest_version else None,
                    item.name,
                )
            except ValueError:
                return model_id, None, item.name
    return model_id, None, name


def _serving_resource_label(data: dict[str, Any]) -> str:
    spec = data.get("resource_spec_price")
    if not isinstance(spec, dict):
        return ""
    gpu_count = spec.get("gpu_count")
    cpu_count = spec.get("cpu_count")
    memory = spec.get("memory_size_gib")
    gpu_info_payload = spec.get("gpu_info")
    gpu_info: dict[str, Any] = (
        gpu_info_payload if isinstance(gpu_info_payload, dict) else {}
    )
    gpu_type = (
        gpu_info.get("gpu_type_display")
        or gpu_info.get("gpu_type")
        or spec.get("gpu_type_display")
        or spec.get("gpu_type")
        or ""
    )
    bits = []
    if cpu_count not in (None, ""):
        bits.append(f"{cpu_count} CPU")
    if memory not in (None, ""):
        bits.append(f"{memory} GiB")
    if gpu_count not in (None, ""):
        gpu = f"{gpu_count} GPU"
        if gpu_type:
            gpu += f" ({gpu_type})"
        bits.append(gpu)
    return ", ".join(bits)


def _format_list_rows(rows: list[dict[str, str]], total: int) -> str:
    """Render a compact, handle-free inference-serving list."""
    del total
    if not rows:
        return "No inference servings found."
    columns = [("name", "Name"), ("status", "Status")]
    columns.extend(
        (key, label)
        for key, label in (
            ("model", "Model"),
            ("replicas", "Replicas"),
            ("project", "Project"),
            ("workspace", "Workspace"),
            ("updated_at", "Updated"),
        )
        if any(row.get(key) not in (None, "", "-") for row in rows)
    )
    table_rows = [
        tuple(row.get(key) or "-" for key, _label in columns)
        for row in rows
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
    return "\n".join([rendered[1], rendered[2], *rendered[3:-1]])


def _public_serving_instance_text(
    item: dict[str, Any],
    *keys: str,
) -> str:
    for key in keys:
        value = item.get(key)
        if value in (None, "") or isinstance(value, (dict, list, tuple, set)):
            continue
        text = scrub_raw_ids(value).strip()
        if text and "<redacted>" not in text:
            return text
    return ""


def _serving_instance_rank(item: dict[str, Any], position: int) -> int:
    for key in ("rank", "instance_rank", "global_rank", "index", "replica_index"):
        value = item.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
    return position


def _serving_instance_resource(item: dict[str, Any]) -> str:
    direct = _public_serving_instance_text(item, "resource")
    if direct:
        return direct

    spec = item
    for key in ("resource_spec", "resource_spec_price", "quota"):
        candidate = item.get(key)
        if isinstance(candidate, dict):
            spec = candidate
            break

    values = (
        ("CPU", _public_serving_instance_text(spec, "cpu_count", "cpu")),
        (
            "GiB",
            _public_serving_instance_text(
                spec,
                "memory_size_gib",
                "memory_gib",
                "memory_size",
                "memory",
            ),
        ),
        ("GPU", _public_serving_instance_text(spec, "gpu_count", "gpu")),
    )
    return ", ".join(f"{value} {unit}" for unit, value in values if value)


def _public_serving_instances(
    instances: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for position, raw in enumerate(instances):
        item: dict[str, Any] = {}
        name = _public_serving_instance_text(
            raw,
            "name",
            "instance_name",
            "display_name",
        )
        if name and not looks_like_platform_id(name):
            item["name"] = name

        for key, candidates in (
            ("status", ("status", "instance_status", "phase", "state")),
            ("role", ("role", "instance_type", "component")),
            ("type", ("type",)),
        ):
            value = _public_serving_instance_text(raw, *candidates)
            if value:
                item[key] = value

        resource = _serving_instance_resource(raw)
        if resource:
            item["resource"] = resource
        item["rank"] = _serving_instance_rank(raw, position)
        projected.append(item)
    return projected


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
    return "\n".join([rendered[1], rendered[2], *rendered[3:-1]])


def _public_serving_version(item: dict[str, Any]) -> dict[str, Any]:
    """Project one `ListServingVersions` row onto rollback-relevant fields."""
    view: dict[str, Any] = {}
    raw_version = item.get("version")
    if raw_version not in (None, ""):
        try:
            view["version"] = int(str(raw_version))
        except (TypeError, ValueError):
            view["version"] = scrub_raw_ids(raw_version)
    for key, candidates in (
        ("status", ("status", "phase")),
        ("model", ("model_name", "model_display_name")),
        ("command", ("command",)),
        ("created_at", ("created_at", "updated_at")),
    ):
        value = _public_serving_instance_text(item, *candidates)
        if value:
            view[key] = value
    for key, candidates in (
        ("replicas", ("replicas", "replica_count")),
        ("port", ("port",)),
    ):
        for candidate in candidates:
            raw = item.get(candidate)
            if raw not in (None, ""):
                try:
                    view[key] = int(str(raw))
                except (TypeError, ValueError):
                    pass
                break
    resource = _serving_resource_label(item)
    if resource:
        view["resource"] = resource
    return view


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
    return "\n".join([rendered[1], rendered[2], *rendered[3:-1]])


def _scale_replica_count(item: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        raw = item.get(key)
        if raw in (None, "") or isinstance(raw, bool):
            continue
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            continue
    return None


def _public_scale_history_entry(item: dict[str, Any]) -> dict[str, Any]:
    """Project one `ListServingScaleHistory` row onto the replica delta.

    The row's `id` is an internal counter with nothing to look up, so it is
    dropped; what answers "why did latency move" is when the replica count
    changed and what it changed from and to.
    """
    view: dict[str, Any] = {}
    before = _scale_replica_count(
        item, "replicas_before_scale", "replicas_before", "before_replicas"
    )
    after = _scale_replica_count(
        item, "replicas_after_scale", "replicas_after", "after_replicas"
    )
    if before is not None:
        view["replicas_from"] = before
    if after is not None:
        view["replicas_to"] = after
    status = _public_serving_instance_text(item, "status", "state", "phase")
    if status:
        view["status"] = status
    created_at = human_formatter.format_epoch(
        item.get("created_at") or item.get("updated_at") or ""
    )
    if created_at not in ("", "-"):
        view["created_at"] = scrub_raw_ids(created_at)
    return view


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
    return "\n".join([rendered[1], rendered[2], *rendered[3:-1]])


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

    \b
    Examples:
        inspire serving list --workspace 分布式训练空间 --project CI-情境智能
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
                    }
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

    Detail includes status, project, model, image, resource, startup command,
    port, replicas, and timestamps when the platform returns them.
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
            click.echo(json_formatter.format_json(detail))
            return

        detail = public_serving(data, fallback_name=name)
        lines = [
            f"Name: {detail.get('name') or name}",
            f"Status: {detail.get('status') or 'N/A'}",
        ]
        for key, label in (
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
            ("command", "Command"),
            ("port", "Port"),
            ("created_at", "Created"),
            ("updated_at", "Updated"),
        ):
            value = detail.get(key)
            if value not in (None, ""):
                lines.append(f"{label}: {value}")
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

    \b
    Scaling reuses the deployment's existing image, command, port and resource
    spec — only the replica count moves. Each replica costs the serving's full
    quota, so check `inspire resources quota --workspace <workspace>` before
    scaling up. Watch the result with
    `inspire serving instances <name> --workspace <workspace>`.
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

    \b
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

    \b
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

    \b
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
    "--tail",
    type=click.IntRange(1),
    default=DEFAULT_EVENT_TAIL,
    show_default=True,
    help="Maximum recent events to display.",
)
@click.option("--follow", "-f", is_flag=True, help="Follow and print new events.")
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
    tail: int,
    follow: bool,
    interval: int,
) -> None:
    """Show lifecycle and scheduling events for an inference serving."""
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
        run_events_command(
            ctx,
            fetch=lambda: _run_readonly_serving_operation(
                ctx,
                name=name,
                workspace_id=workspace_id,
                session=session,
                pick=pick,
                operation=lambda serving_id, live_session: (
                    browser_api_module.list_serving_events(
                        serving_id,
                        session=live_session,
                    )
                ),
            ),
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
    metavar="NAME|all",
    help="Workspace name or 'all'.",
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
        workspace_ids, all_workspaces = resolve_workspace_query_scope(
            workspace=workspace,
            session=session,
        )
        workspace_names = workspace_name_map(session)
        if all_workspaces:
            items: list[dict[str, Any]] = []
            for workspace_id in workspace_ids:
                public_data = public_configs(
                    browser_api_module.get_serving_configs(
                        workspace_id=workspace_id,
                        session=session,
                    )
                )
                workspace_name = scrub_raw_ids(
                    workspace_names.get(workspace_id) or "(workspace name unavailable)"
                )
                for item in public_data.get("items", []):
                    scoped_item = {
                        "workspace": workspace_name,
                        **item,
                    }
                    if "auto_stop" in public_data:
                        scoped_item["auto_stop"] = public_data["auto_stop"]
                    items.append(scoped_item)
            page = bound_collection(items, limit=effective_limit)
            output = {
                "items": page.items,
                **page.metadata(),
            }
        else:
            data = browser_api_module.get_serving_configs(
                workspace_id=workspace_ids[0],
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
            page = bound_collection(
                items,
                limit=effective_limit,
            )
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
    help="Registered model name",
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
        "Required unless supplied by --profile."
    ),
)
@click.option(
    "--quota",
    "-q",
    metavar="SPEC",
    help="Serving resource as gpu,cpu,mem. Required unless supplied by --profile.",
)
@click.option(
    "--image",
    "-i",
    metavar="NAME|URL",
    help="Visible image name or name:tag. Required unless supplied by --profile.",
)
@click.option(
    "--profile",
    "profile_name",
    default=None,
    metavar="NAME",
    help="Serving condition profile providing workspace/project/group/quota/image.",
)
@click.option("--replicas", type=click.IntRange(1), default=1, show_default=True)
@click.option("--nodes-per-replica", type=click.IntRange(1), default=1, show_default=True)
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
    profile_name: Optional[str],
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
        inspire serving create --name qwen-demo --model qwen-demo --workspace 分布式训练空间 \
          --project CI-情境智能 --group H200-2号机房 --quota 1,18,200 \
          --image serve-base:v1 --command "python serve.py" --port 8000 --dry-run
        inspire serving metrics qwen-demo --workspace 分布式训练空间 --window 30m
    """
    try:
        from inspire.cli.utils.quota_resolver import (
            QuotaMatchError,
            QuotaParseError,
            SCHEDULE_TYPE_SERVING,
            parse_quota,
            resolve_quota,
        )

        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()

        fields = apply_workload_profile(
            profiles=getattr(config, "profiles", {}),
            kind="serving",
            profile_name=profile_name,
            values={
                "workspace": workspace,
                "project": project,
                "group": group,
                "image": image,
                "quota": quota,
            },
        )
        workspace = cast(Optional[str], fields["workspace"])
        project = cast(Optional[str], fields["project"])
        group = cast(Optional[str], fields["group"])
        image = cast(Optional[str], fields["image"])
        quota = cast(Optional[str], fields["quota"])
        for field_name, value in (
            ("workspace", workspace),
            ("project", project),
            ("group", group),
            ("quota", quota),
            ("image", image),
        ):
            if not value:
                raise ConfigError(profile_required_message("serving", field_name))
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
            raise ConfigError(profile_required_message("serving", "workspace"))
        project_id = _resolve_project_id(
            ctx=ctx,
            workspace_id=workspace_id,
            session=session,
            config=config,
            requested=project,
        )
        if not project_id:
            raise ConfigError(profile_required_message("serving", "project"))
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

        mirror_id, image_label = _resolve_image_for_create(image, session=session)
        resource_spec_price = _build_resource_spec_price(resolved)
        final_priority = resolve_workspace_task_priority(
            priority,
            session=session,
            workspace_id=workspace_id,
            project_id=project_id,
        )
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
        if ctx.json_output:
            click.echo(json_formatter.format_json(public_operation(name, "created")))
            return
        click.echo(human_formatter.format_mutation_success("Serving", "created", name))

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
