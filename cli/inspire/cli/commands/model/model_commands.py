"""`inspire model` subcommands — model repository workflows."""

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
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.human_formatter import format_epoch
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.auth import AuthenticationError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import (
    remember_resource_identity,
    resolve_by_name,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import get_web_session


def _resolve_workspace_id(config: Config, workspace: Optional[str], *, session=None) -> Optional[str]:
    if workspace is None:
        return None
    return select_workspace_id(config, explicit_workspace_name=workspace, session=session)


def _resolve_project_id(
    config: Config,
    requested: Optional[str],
    *,
    workspace_id: Optional[str],
    session,
) -> Optional[str]:
    if not requested:
        return None
    if requested.startswith("project-"):
        raise ConfigError(
            "--project takes a project name. "
            "See `inspire project list` or `inspire config context`."
        )

    requested_names = [requested]
    configured = str(config.projects.get(requested) or "").strip()
    if configured:
        if configured.startswith("project-"):
            metadata = config.project_catalog.get(configured)
            if isinstance(metadata, dict):
                configured_name = str(metadata.get("name") or "").strip()
                if configured_name:
                    requested_names.insert(0, configured_name)
        else:
            requested_names.insert(0, configured)

    catalog_entry = config.project_catalog.get(requested)
    if isinstance(catalog_entry, dict):
        catalog_name = str(catalog_entry.get("name") or "").strip()
        if catalog_name:
            requested_names.insert(0, catalog_name)

    projects = browser_api_module.list_projects(
        workspace_id=workspace_id, session=session
    )
    targets = {name.casefold() for name in requested_names if name}
    matches = [project for project in projects if project.name.casefold() in targets]
    if len(matches) == 1:
        return matches[0].project_id
    if len(matches) > 1:
        raise ConfigError(
            f"Project name {requested!r} is ambiguous in the selected workspace."
        )

    available = ", ".join(sorted({project.name for project in projects if project.name}))
    raise ConfigError(
        f"Unknown project name {requested!r}. Available: {available or '(none)'}."
    )


def _current_user_id(session) -> str:  # noqa: ANN001
    user = browser_api_module.get_current_user(session=session)
    user_id = str(user.get("id") or user.get("user_id") or "").strip()
    if not user_id:
        raise ConfigError("Cannot determine the current user from the live web session.")
    return user_id


def _status_label(value: Any) -> str:
    mapping = {
        "0": "PENDING",
        "1": "CREATING",
        "2": "SUCCESS",
        "3": "FAILED",
    }
    if value is None or value == "":
        raw = ""
    else:
        raw = str(value).strip()
    return mapping.get(raw, raw or "-")


def _format_size_gi(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number <= 0:
        return "-"
    if number >= 1024:
        return f"{number / 1024:.2f} TiB"
    return f"{number:.2f} GiB"


def _version_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return f"V{text[1:]}" if text[:1].casefold() == "v" else f"V{text}"


def _created_model_id(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("model_id", "id"):
        candidate = str(value.get(key) or "").strip()
        if candidate:
            return candidate
    for key in ("model", "data", "result"):
        candidate = _created_model_id(value.get(key))
        if candidate:
            return candidate
    return ""


def _string_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [
            scrub_raw_ids(item)
            for item in value
            if str(item or "").strip()
        ]
    text = scrub_raw_ids(value).strip()
    return [text] if text else []


def _format_model_rows(rows: list[dict[str, str]]) -> str:
    """Render a compact model-registry list."""
    if not rows:
        return "No models found."
    values = [
        (
            row["name"],
            row["version"],
            row["status"],
            row["project"],
            row["updated_at"],
        )
        for row in rows
    ]
    widths = [
        column_width("Name", [row[0] for row in values], max_width=48),
        column_width("Version", [row[1] for row in values], max_width=12),
        column_width("Status", [row[2] for row in values], max_width=16),
        column_width("Project", [row[3] for row in values], max_width=36),
        column_width("Updated", [row[4] for row in values], max_width=20),
    ]
    return "\n".join(
        render_table(
            ("Name", "Version", "Status", "Project", "Updated"),
            values,
            widths,
            line_char="─",
        )
    )


def _model_list_view(model: browser_api_module.ModelInfo) -> dict[str, str]:
    view = {
        "name": scrub_raw_ids(model.name),
        "version": scrub_raw_ids(_version_label(model.latest_version)),
        "status": scrub_raw_ids(_status_label(model.status)),
        "project": scrub_raw_ids(model.project_name),
        "updated_at": scrub_raw_ids(
            format_epoch(model.updated_at) if model.updated_at else ""
        ),
    }
    return {key: value for key, value in view.items() if value and value != "-"}


def _version_inner(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    model_payload = item.get("model")
    return model_payload if isinstance(model_payload, dict) else item


def _version_items(data: Any) -> list[dict[str, Any]]:
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _latest_version(data: Any) -> dict[str, Any]:
    def _key(item: dict[str, Any]) -> int:
        inner = _version_inner(item)
        try:
            return int(inner.get("version") or inner.get("model_version") or 0)
        except (TypeError, ValueError):
            return 0

    latest = max(_version_items(data), key=_key, default={})
    return _version_inner(latest)


def _model_detail_view(
    name: str,
    data: dict[str, Any],
    version_data: dict[str, Any],
) -> dict[str, Any]:
    model_payload = data.get("model")
    inner: dict[str, Any] = model_payload if isinstance(model_payload, dict) else data
    latest = _latest_version(version_data)
    version = latest.get("version") or inner.get("version")
    view: dict[str, Any] = {
        "name": scrub_raw_ids(inner.get("name") or name),
        "status": scrub_raw_ids(
            _status_label(latest.get("status", inner.get("status")))
        ),
        "version": _version_label(version),
        "description": scrub_raw_ids(inner.get("description") or ""),
        "type": _string_values(inner.get("model_type")),
        "tags": _string_values(inner.get("tags")),
        "vllm_ready": bool(
            latest.get("is_vllm_compatible", inner.get("is_vllm_compatible"))
        ),
        "published": bool(inner.get("has_published")),
        "project": scrub_raw_ids(data.get("project_name") or ""),
        "owner": scrub_raw_ids(data.get("user_name") or ""),
        "created_at": (
            format_epoch(inner.get("created_at")) if inner.get("created_at") else ""
        ),
        "updated_at": (
            format_epoch(inner.get("updated_at")) if inner.get("updated_at") else ""
        ),
    }
    return {
        key: value
        for key, value in view.items()
        if value not in ("", None, []) or key in {"vllm_ready", "published"}
    }


def _model_version_views(data: dict[str, Any]) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for item in _version_items(data):
        inner = _version_inner(item)
        version = inner.get("version") or inner.get("model_version")
        view: dict[str, Any] = {
            "version": _version_label(version),
            "status": scrub_raw_ids(_status_label(inner.get("status") or item.get("status"))),
            "size": _format_size_gi(
                inner.get("model_size_gi")
                or inner.get("model_size_gb")
                or inner.get("size")
            ),
            "vllm_ready": bool(inner.get("is_vllm_compatible")),
        }
        running = item.get("running_infrence_serving")
        if running not in (None, ""):
            view["running_servings"] = running
        views.append(
            {
                key: value
                for key, value in view.items()
                if value not in ("", None, "-") or key == "vllm_ready"
            }
        )
    return views


def _format_model_detail(view: dict[str, Any]) -> str:
    labels = (
        ("Name", "name"),
        ("Status", "status"),
        ("Version", "version"),
        ("Description", "description"),
        ("Type", "type"),
        ("Tags", "tags"),
        ("vLLM-ready", "vllm_ready"),
        ("Published", "published"),
        ("Project", "project"),
        ("Owner", "owner"),
        ("Created", "created_at"),
        ("Updated", "updated_at"),
    )
    lines: list[str] = []
    for label, key in labels:
        if key not in view:
            continue
        value = view[key]
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        elif isinstance(value, bool):
            value = "yes" if value else "no"
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _format_model_versions(versions: list[dict[str, Any]]) -> str:
    if not versions:
        return ""
    rows = [
        (
            str(version.get("version") or ""),
            str(version.get("status") or ""),
            str(version.get("size") or ""),
            "yes" if version.get("vllm_ready") else "no",
            str(version.get("running_servings") or ""),
        )
        for version in versions
    ]
    widths = [
        column_width("Version", [row[0] for row in rows], max_width=12),
        column_width("Status", [row[1] for row in rows], max_width=16),
        column_width("Size", [row[2] for row in rows], max_width=14),
        column_width("vLLM", [row[3] for row in rows], max_width=6),
        column_width("Servings", [row[4] for row in rows], max_width=10),
    ]
    return "\n".join(
        render_table(
            ("Version", "Status", "Size", "vLLM", "Servings"),
            rows,
            widths,
            line_char="─",
        )
    )


def _resolve_model_name(
    ctx: Context,
    name: str,
    *,
    workspace_id: Optional[str] = None,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    pick: Optional[int] = None,
    session=None,  # noqa: ANN001
    require_live: bool = False,
) -> str:
    live_session = session or get_web_session()

    def _lister():
        items, _ = browser_api_module.list_models(
            workspace_id=workspace_id,
            page=1,
            page_size=100,
            keyword=name,
            project_ids=[project_id] if project_id else None,
            user_id=user_id,
            session=live_session,
        )
        return [
            {
                "name": m.name,
                "id": m.model_id,
                "status": _status_label(m.status),
                "project": m.project_name,
                "created_at": format_epoch(m.created_at) if m.created_at else "",
            }
            for m in items
        ]

    return resolve_by_name(
        ctx,
        name=name,
        resource_type="model",
        list_candidates=_lister,
        json_output=ctx.json_output,
        pick_index=pick,
        session=live_session,
        workspace_id=str(workspace_id or ""),
        owner_scope="self",
        require_live=require_live,
        list_command="inspire model list --workspace <workspace>",
    )


@click.command("list")
@click.option("--workspace", required=True, help="Workspace name")
@click.option("--project", default=None, help="Project name filter")
@click.option("--keyword", default=None, help="Server-side model name/description search")
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=100,
    show_default=True,
    help="Maximum models to query and display.",
)
@pass_context
def list_model(
    ctx: Context,
    workspace: Optional[str],
    project: Optional[str],
    keyword: Optional[str],
    limit: int,
) -> None:
    """List registered models owned by the current user.

    Use filters to narrow by workspace, project, or keyword. After finding a
    candidate model, use `model status` for metadata and `model versions` to
    choose the version for serving or reproducibility.
    """
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        resolved_workspace = _resolve_workspace_id(config, workspace, session=session)
        project_id = _resolve_project_id(
            config, project, workspace_id=resolved_workspace, session=session
        )
        user_id = _current_user_id(session)
        items, _ = browser_api_module.list_models(
            workspace_id=resolved_workspace,
            page=1,
            page_size=limit,
            keyword=keyword,
            project_ids=[project_id] if project_id else None,
            user_id=user_id,
            session=session,
        )

        views = [_model_list_view(model) for model in items]
        for model in items:
            remember_resource_identity(
                session=session,
                resource_type="model",
                resource_id=model.model_id,
                name=model.name,
                workspace_id=str(resolved_workspace or ""),
                owner_scope="self",
                status=model.status,
                created_at=model.created_at,
            )
        if ctx.json_output:
            click.echo(json_formatter.format_json({"models": views}))
            return

        rows = [
            {
                "name": view.get("name", "-"),
                "version": view.get("version", "-"),
                "status": view.get("status", "-"),
                "project": view.get("project", "-"),
                "updated_at": view.get("updated_at", "-"),
            }
            for view in views
        ]
        click.echo(_format_model_rows(rows))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)


@click.command("status")
@click.argument("name")
@click.option("--workspace", required=True, help="Workspace name")
@click.option("--project", default=None, help="Project name filter")
@click.option("--pick", type=click.IntRange(1), default=None, help="Pick Nth duplicate name (1-indexed)")
@pass_context
def status_model(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    project: Optional[str],
    pick: Optional[int],
) -> None:
    """Show detail of one registered model by name.

    Includes latest version status, tags, model type, vLLM readiness,
    publication flag, owner, project, and timestamps when present.
    """
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(config, workspace, session=session)
        project_id = _resolve_project_id(
            config, project, workspace_id=workspace_id, session=session
        )
        user_id = _current_user_id(session)
        model_id = _resolve_model_name(
            ctx,
            name,
            workspace_id=workspace_id,
            project_id=project_id,
            user_id=user_id,
            pick=pick,
            session=session,
        )
        data = browser_api_module.get_model_detail(
            model_id=model_id, session=session, workspace_id=workspace_id
        )
        version_data = browser_api_module.list_model_version_records(
            model_id=model_id, session=session, workspace_id=workspace_id
        )
        remember_resource_identity(
            session=session,
            resource_type="model",
            resource_id=model_id,
            name=name,
            workspace_id=str(workspace_id or ""),
            owner_scope="self",
        )

        view = _model_detail_view(name, data, version_data)
        if ctx.json_output:
            click.echo(json_formatter.format_json(view))
            return

        click.echo(_format_model_detail(view))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)


@click.command("versions")
@click.argument("name")
@click.option("--workspace", required=True, help="Workspace name")
@click.option("--project", default=None, help="Project name filter")
@click.option("--pick", type=click.IntRange(1), default=None, help="Pick Nth duplicate name (1-indexed)")
@pass_context
def versions_model(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    project: Optional[str],
    pick: Optional[int],
) -> None:
    """List all versions of one registered model by name.

    Use this before `serving create` when you need a specific
    `--model-version`; omit the version on serving create to use the latest
    version shown by model listing.
    """
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(config, workspace, session=session)
        project_id = _resolve_project_id(
            config, project, workspace_id=workspace_id, session=session
        )
        user_id = _current_user_id(session)
        model_id = _resolve_model_name(
            ctx,
            name,
            workspace_id=workspace_id,
            project_id=project_id,
            user_id=user_id,
            pick=pick,
            session=session,
        )
        data = browser_api_module.list_model_version_records(
            model_id=model_id, session=session, workspace_id=workspace_id
        )
        remember_resource_identity(
            session=session,
            resource_type="model",
            resource_id=model_id,
            name=name,
            workspace_id=str(workspace_id or ""),
            owner_scope="self",
        )

        versions = _model_version_views(data)
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"name": scrub_raw_ids(name), "versions": versions}
                )
            )
            return

        if not versions:
            click.echo(f"No versions for model {scrub_raw_ids(name)}.")
            return

        click.echo(_format_model_versions(versions))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)


@click.command("register")
@click.option("--name", "-n", required=True, help="Model name")
@click.option("--source-path", required=True, help="Platform-visible model directory on shared storage")
@click.option("--workspace", required=True, help="Workspace name.")
@click.option(
    "--project",
    "-p",
    required=True,
    help="Project name.",
)
@click.option(
    "--type",
    "model_type",
    multiple=True,
    help="Model type segment; pass twice for category + task",
)
@click.option("--tag", "tags", multiple=True, help="Custom model tag")
@click.option("--description", default="", help="Model description")
@pass_context
def register_model(
    ctx: Context,
    name: str,
    source_path: str,
    workspace: str,
    project: Optional[str],
    model_type: tuple[str, ...],
    tags: tuple[str, ...],
    description: str,
) -> None:
    """Register a platform-visible model directory in the model repository.

    This creates the model entry from an existing shared-storage directory.
    It does not upload local files; copy or generate model files on the
    platform first, then pass that remote directory as `--source-path`.
    """
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(config, workspace, session=session)
        if not workspace_id:
            raise ConfigError("Missing workspace.")
        requested_project = project
        project_id: Optional[str]
        project_id = _resolve_project_id(
            config,
            requested_project,
            workspace_id=workspace_id,
            session=session,
        )
        if not project_id:
            raise ConfigError("--project is required.")

        result = browser_api_module.create_model(
            name=name,
            project_id=project_id,
            workspace_id=workspace_id,
            model_source_path=source_path,
            model_type=model_type,
            tags=tags,
            description=description,
            model_source_type=1,
            session=session,
        )
        model_id = _created_model_id(result)
        remember_resource_identity(
            session=session,
            resource_type="model",
            resource_id=model_id,
            name=name,
            workspace_id=str(workspace_id or ""),
            owner_scope="self",
        )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "name": scrub_raw_ids(name),
                        "status": "registered",
                        "project": scrub_raw_ids(project or ""),
                        "workspace": scrub_raw_ids(workspace),
                    }
                )
            )
            return

        click.echo(f"Model registered: {scrub_raw_ids(name)}")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)


__all__ = ["list_model", "register_model", "status_model", "versions_model"]
