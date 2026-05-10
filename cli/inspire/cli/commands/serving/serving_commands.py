"""`inspire serving` subcommands."""

from __future__ import annotations

from typing import Any, Optional, cast

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.formatters.human_formatter import format_epoch
from inspire.cli.utils.auth import AuthenticationError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import resolve_by_name
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workload_profiles import apply_workload_profile, profile_required_message
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import get_web_session


def _resolve_serving_name(
    ctx: Context,
    name: str,
    *,
    workspace_id: Optional[str] = None,
    pick: Optional[int] = None,
) -> str:
    """Resolve a serving name to its platform id (``sv-<uuid>``).

    Scope: ``my_serving=True`` (default) × session workspace, full page.
    """

    def _lister():
        session = get_web_session()
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
        json_output=ctx.json_output,
        pick_index=pick,
    )


def _resolve_workspace_id(config: Config, workspace: Optional[str], *, session=None) -> Optional[str]:
    if workspace is None:
        return None
    return select_workspace_id(config, explicit_workspace_name=workspace, session=session)


def _resolve_project_id(
    *,
    workspace_id: Optional[str],
    session,
    config: Config,
    requested: Optional[str],
    allow_config_raw_id: bool = False,
) -> Optional[str]:
    if not requested:
        return None
    if requested.startswith("project-"):
        if allow_config_raw_id:
            return requested
        raise ConfigError(
            "--project takes a project name. "
            "See `inspire project list` or `inspire config context`."
        )
    if requested in config.projects:
        return config.projects[requested]
    for project_id, metadata in config.project_catalog.items():
        if metadata.get("name") == requested:
            return project_id
    data = browser_api_module.list_serving_user_project(
        workspace_id=workspace_id, session=session
    )
    for item in data.get("projects") or []:
        if item.get("project_name") == requested or item.get("name") == requested:
            return str(item.get("project_id") or item.get("id") or "")
    raise ConfigError(f"Unknown project: {requested!r}.")


def _resolve_image_for_create(raw: str, *, session, ctx: Context) -> tuple[str, str]:
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
                image_id = str(img.image_id or "").strip()
                if image_id:
                    display = f"{img.name}:{img.version}" if img.name and img.version else raw
                    return image_id, display
                break
    raise ConfigError(f"Unknown image: {raw!r}.")


def _resolve_image_id(raw: str, *, session, ctx: Context) -> str:
    image_id, _display = _resolve_image_for_create(raw, session=session, ctx=ctx)
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
        json_output=ctx.json_output,
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


def _serving_model_label(data: dict[str, Any]) -> str:
    model_payload = data.get("model")
    if isinstance(model_payload, dict):
        name = (
            model_payload.get("name")
            or model_payload.get("model_name")
            or data.get("model_name")
            or data.get("model_display_name")
        )
        version = (
            model_payload.get("version")
            or model_payload.get("model_version")
            or data.get("model_version")
        )
    else:
        name = data.get("model_name") or model_payload or data.get("model_display_name")
        version = data.get("model_version")
    if not name:
        return ""
    return f"{name} v{version}" if version not in (None, "") else str(name)


def _serving_image_label(data: dict[str, Any]) -> str:
    mirror_payload = data.get("mirror")
    if isinstance(mirror_payload, dict):
        name = mirror_payload.get("name") or mirror_payload.get("image_name")
        version = mirror_payload.get("version")
        url = mirror_payload.get("address") or mirror_payload.get("url")
        if name and version:
            return f"{name}:{version}"
        return str(name or url or "")
    return str(data.get("image") or data.get("mirror_url") or data.get("image_url") or "")


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
    """Render an inference-serving list.

    ``total`` is the server-reported total across all pages; it may be larger
    than ``len(rows)`` when the caller is paginating. The footer prints
    ``Showing X of Y`` in that case so users are not misled into thinking
    they have a complete view.
    """
    if not rows:
        return "No inference servings found."
    widths = {
        col: max(len(col.title().replace("_", " ")), *(len(r[col]) for r in rows))
        for col in ("name", "status", "model", "replicas", "project", "updated_at")
    }
    header = (
        f"{'Name':<{widths['name']}}  "
        f"{'Status':<{widths['status']}}  "
        f"{'Model':<{widths['model']}}  "
        f"{'Replicas':<{widths['replicas']}}  "
        f"{'Project':<{widths['project']}}  "
        f"{'Updated':<{widths['updated_at']}}"
    )
    sep = "-" * len(header)
    lines = ["Inference Servings", header, sep]
    for r in rows:
        lines.append(
            f"{r['name']:<{widths['name']}}  "
            f"{r['status']:<{widths['status']}}  "
            f"{r['model']:<{widths['model']}}  "
            f"{r['replicas']:<{widths['replicas']}}  "
            f"{r['project']:<{widths['project']}}  "
            f"{r['updated_at']:<{widths['updated_at']}}"
        )
    lines.append(sep)
    if total > len(rows):
        lines.append(f"Showing {len(rows)} of {total}")
    else:
        lines.append(f"Total: {len(rows)}")
    return "\n".join(lines)


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
        return scrub_raw_ids(rule)
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
    return ", ".join(conds) if conds else scrub_raw_ids(rule)


def _format_configs(data: dict[str, Any]) -> str:
    configs = data.get("configs") if isinstance(data, dict) else None
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
    lines = ["Available Inference Serving Configs"]
    if enable_auto_stop is not None:
        lines.append(f"Auto-stop: {'enabled' if enable_auto_stop else 'disabled'}")
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            lines.append(f"[{i}] {scrub_raw_ids(item)}")
            continue
        gpu_min = item.get("gpu_count_min")
        gpu_max = item.get("gpu_count_max")
        bits = []
        if gpu_min is not None or gpu_max is not None:
            bits.append(f"gpu={gpu_min or '?'}-{gpu_max or '?'}")
        rule = _format_auto_stop(str(item.get("auto_stop_ruleset") or ""))
        if rule != "-":
            bits.append(f"auto_stop={rule}")
        lines.append(f"[{i}] " + (", ".join(bits) if bits else _config_label(item, i)))
    return "\n".join(lines)


@click.command("list")
@click.option("--workspace", default=None, help="Workspace name")
@click.option("--project", default=None, help="Project name filter")
@click.option("--status", "status_filter", default=None, help="Serving status filter")
@click.option("--keyword", default=None, help="Server-side name/model search")
@click.option("--page", type=int, default=1, show_default=True)
@click.option("--page-size", type=int, default=50, show_default=True)
@pass_context
def list_serving(
    ctx: Context,
    workspace: Optional[str],
    project: Optional[str],
    status_filter: Optional[str],
    keyword: Optional[str],
    page: int,
    page_size: int,
) -> None:
    """List inference servings in the current (or given) workspace."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        resolved_workspace = _resolve_workspace_id(config, workspace)

        session = get_web_session()
        project_id = _resolve_project_id(
            workspace_id=resolved_workspace,
            session=session,
            config=config,
            requested=project,
        )
        items, total = browser_api_module.list_servings(
            workspace_id=resolved_workspace,
            keyword=keyword,
            project_ids=[project_id] if project_id else None,
            statuses=[status_filter] if status_filter else None,
            page=page,
            page_size=page_size,
            session=session,
        )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "total": total,
                        "items": [s.raw if s.raw else s.__dict__ for s in items],
                    }
                )
            )
            return

        rows = [
            {
                "id": s.inference_serving_id or "-",
                "name": scrub_raw_ids(s.name or "-"),
                "status": scrub_raw_ids(s.status or "-"),
                "model": scrub_raw_ids(
                    f"{s.model_name} v{s.model_version}".strip()
                    if s.model_name
                    else "-"
                ),
                "replicas": (
                    f"{s.replicas}x{s.node_num_per_replica}"
                    if s.node_num_per_replica
                    else str(s.replicas or "-")
                ),
                "project": scrub_raw_ids(s.project_name or "-"),
                "updated_at": scrub_raw_ids(s.updated_at or s.created_at or "-"),
            }
            for s in items
        ]
        click.echo(_format_list_rows(rows, total=int(total) if total is not None else len(rows)))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("status")
@click.argument("name")
@click.option("--workspace", default=None, help="Workspace name")
@click.option("--pick", type=int, default=None, help="Pick Nth duplicate name (1-indexed)")
@pass_context
def status_serving(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    pick: Optional[int],
) -> None:
    """Get detail of an inference serving (pass the serving name)."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        workspace_id = _resolve_workspace_id(config, workspace)
        session = get_web_session()
        inference_serving_id = _resolve_serving_name(
            ctx,
            name,
            workspace_id=workspace_id,
            pick=pick,
        )
        data = browser_api_module.get_serving_detail(
            inference_serving_id=inference_serving_id,
            session=session,
        )

        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return

        click.echo("Inference Serving Status")
        click.echo(f"Name:     {scrub_raw_ids(data.get('name', 'N/A'))}")
        click.echo(f"Status:   {scrub_raw_ids(data.get('status', 'N/A'))}")
        if data.get("inference_serving_type"):
            click.echo(f"Type:     {scrub_raw_ids(data.get('inference_serving_type'))}")
        project_payload = data.get("project")
        project_name = (
            project_payload.get("name")
            if isinstance(project_payload, dict)
            else data.get("project_name")
        )
        if project_name:
            click.echo(f"Project:  {scrub_raw_ids(project_name)}")
        if data.get("replicas") is not None:
            click.echo(f"Replicas: {data.get('replicas')}")
        if data.get("node_num_per_replica") is not None:
            click.echo(f"Nodes/rep: {data.get('node_num_per_replica')}")
        if data.get("task_priority") not in (None, ""):
            click.echo(f"Priority: {data.get('task_priority')}")
        image_label = _serving_image_label(data)
        if image_label:
            click.echo(f"Image:    {scrub_raw_ids(image_label)}")
        model_label = _serving_model_label(data)
        if model_label:
            click.echo(f"Model:    {scrub_raw_ids(model_label)}")
        resource_label = _serving_resource_label(data)
        if resource_label:
            click.echo(f"Resource: {scrub_raw_ids(resource_label)}")
        if data.get("command"):
            click.echo(f"Command:  {scrub_raw_ids(data.get('command'))}")
        if data.get("port") not in (None, ""):
            click.echo(f"Port:     {data.get('port')}")
        if data.get("created_at"):
            click.echo(f"Created:  {scrub_raw_ids(format_epoch(data.get('created_at')))}")
        if data.get("updated_at"):
            click.echo(f"Updated:  {scrub_raw_ids(format_epoch(data.get('updated_at')))}")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("stop")
@click.argument("name")
@click.option("--workspace", default=None, help="Workspace name")
@click.option(
    "--pick",
    type=int,
    default=None,
    help="Pick the Nth candidate (1-indexed) when the name is ambiguous.",
)
@pass_context
def stop_serving(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    pick: Optional[int],
) -> None:
    """Stop an inference serving (pass the serving name)."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(config, workspace)
        inference_serving_id = _resolve_serving_name(
            ctx,
            name,
            workspace_id=workspace_id,
            pick=pick,
        )
        data = browser_api_module.stop_serving(
            inference_serving_id=inference_serving_id,
            session=session,
        )

        if ctx.json_output:
            click.echo(json_formatter.format_json({"name": name, "stopped": True, **data}))
            return

        click.echo(human_formatter.format_success(f"Inference serving stopped: {name}"))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("delete")
@click.argument("name")
@click.option("--workspace", default=None, help="Workspace name")
@click.option(
    "--pick",
    type=int,
    default=None,
    help="Pick the Nth candidate (1-indexed) when the name is ambiguous.",
)
@pass_context
def delete_serving(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    pick: Optional[int],
) -> None:
    """Delete an inference serving entry (pass the serving name)."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(config, workspace)
        inference_serving_id = _resolve_serving_name(
            ctx,
            name,
            workspace_id=workspace_id,
            pick=pick,
        )
        data = browser_api_module.delete_serving(
            inference_serving_id=inference_serving_id,
            session=session,
        )

        if ctx.json_output:
            click.echo(json_formatter.format_json({"name": name, "deleted": True, **data}))
            return

        click.echo(human_formatter.format_success(f"Inference serving deleted: {name}"))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("configs")
@click.option("--workspace", default=None, help="Workspace name")
@pass_context
def configs_serving(
    ctx: Context,
    workspace: Optional[str],
) -> None:
    """Show available inference-serving configs (images / specs) for a workspace."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        resolved_workspace = _resolve_workspace_id(config, workspace)

        session = get_web_session()
        data = browser_api_module.get_serving_configs(
            workspace_id=resolved_workspace, session=session
        )

        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return

        click.echo(_format_configs(data))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("create")
@click.option("--name", "-n", required=True, help="Serving name")
@click.option("--model", "model_name", required=True, help="Registered model name")
@click.option(
    "--model-version",
    type=int,
    default=None,
    help="Model version (default: latest version from model list)",
)
@click.option("--workspace", help="Workspace name. Required unless supplied by --profile.")
@click.option(
    "--project",
    "-p",
    help="Project name. Required unless supplied by --profile.",
)
@click.option("--group", help="Compute group name. Required unless supplied by --profile.")
@click.option("--quota", "-q", help="Serving spec as gpu,cpu,mem. Required unless supplied by --profile.")
@click.option("--image", help="Visible image name or name:tag. Required unless supplied by --profile.")
@click.option(
    "--profile",
    "profile_name",
    default=None,
    help="Serving condition profile providing workspace/project/group/quota/image.",
)
@click.option("--command", "-c", required=True, help="Serving startup command")
@click.option("--port", type=int, required=True, help="Service port in the container")
@click.option("--replicas", type=int, default=1, show_default=True)
@click.option("--nodes-per-replica", type=int, default=1, show_default=True)
@click.option("--shm-gib", type=int, default=None, help="Shared memory size in GiB")
@click.option(
    "--priority",
    type=click.IntRange(1, 10),
    default=10,
    show_default=True,
    help="Task priority 1-10.",
)
@click.option(
    "--custom-domain",
    default=None,
    help="Optional domain prefix: lowercase letters, digits, and hyphens",
)
@click.option("--description", default="", help="Serving description")
@click.option("--dry-run", is_flag=True, default=False, help="Print payload without creating")
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
    shm_gib: Optional[int],
    priority: Optional[int],
    custom_domain: Optional[str],
    description: str,
    dry_run: bool,
) -> None:
    """Create an inference serving from a registered model."""
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

        workspace_id = select_workspace_id(config, explicit_workspace_name=workspace, session=session)
        if not workspace_id:
            raise ConfigError(profile_required_message("serving", "workspace"))
        project_id = _resolve_project_id(
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

        mirror_id, image_label = _resolve_image_for_create(image, session=session, ctx=ctx)
        resource_spec_price = _build_resource_spec_price(resolved)
        final_priority = priority if priority is not None else 10
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
            "shm_gi": shm_gib,
            "task_priority": final_priority,
            "resource_spec_price": resource_spec_price,
        }
        if custom_domain:
            payload["custom_domain"] = custom_domain

        if dry_run:
            plan = {
                "dry_run": True,
                "kind": "serving",
                "name": name,
                "workspace_name": workspace,
                "project_name": project,
                "compute_group_name": resolved.compute_group_name,
                "image_name": image_label,
                "model_name": model_label,
                "model_version": final_model_version,
                "resource": spec.display(),
                "command": command,
                "description": description,
                "port": port,
                "replicas": replicas,
                "nodes_per_replica": nodes_per_replica,
                "shm_gib": shm_gib,
                "priority": final_priority,
                "custom_domain": custom_domain,
            }
            if ctx.json_output:
                click.echo(json_formatter.format_json(plan))
            else:
                click.echo("Inference Serving Create Plan")
                click.echo(f"Name:      {scrub_raw_ids(name)}")
                click.echo(f"Project:   {scrub_raw_ids(project)}")
                click.echo(f"Workspace: {scrub_raw_ids(workspace)}")
                click.echo(f"Compute:   {scrub_raw_ids(resolved.compute_group_name)}")
                click.echo(f"Resource:  {spec.display()}")
                click.echo(f"Image:     {scrub_raw_ids(image_label)}")
                click.echo(f"Model:     {scrub_raw_ids(model_label)} v{final_model_version}")
                click.echo(f"Command:   {scrub_raw_ids(command)}")
                click.echo(f"Port:      {port}")
                click.echo(f"Replicas:  {replicas} x {nodes_per_replica} node(s)")
                if shm_gib is not None:
                    click.echo(f"SHM:       {shm_gib} GiB")
                click.echo(f"Priority:  {final_priority}")
                if custom_domain:
                    click.echo(f"Domain:    {scrub_raw_ids(custom_domain)}")
                click.echo("No create API call was made.")
            return

        data = browser_api_module.create_serving(
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
            shm_gi=shm_gib,
            task_priority=final_priority,
            custom_domain=custom_domain,
            resource_spec_price=resource_spec_price,
            session=session,
        )
        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return
        click.echo(human_formatter.format_success(f"Inference serving created: {name}"))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = [
    "create_serving",
    "delete_serving",
    "list_serving",
    "status_serving",
    "stop_serving",
    "configs_serving",
]
