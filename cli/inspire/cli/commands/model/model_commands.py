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
from inspire.cli.utils.auth import AuthenticationError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import resolve_by_name
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
    if requested in config.projects:
        return config.projects[requested]
    for project_id, metadata in config.project_catalog.items():
        if metadata.get("name") == requested:
            return project_id
    for project in browser_api_module.list_projects(
        workspace_id=workspace_id, session=session
    ):
        if project.name == requested:
            return project.project_id
    raise ConfigError(f"Unknown project: {requested!r}.")


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


def _join_values(values: Any) -> str:
    if isinstance(values, (list, tuple)):
        return ", ".join(str(v) for v in values if str(v).strip())
    return str(values or "")


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


def _format_model_rows(rows: list[dict[str, str]], total: int) -> str:
    """Render a model-registry list.

    ``total`` is the server-reported total across pages; the footer prints
    ``Showing X of Y`` when ``len(rows) < total`` so paginating users don't
    confuse the visible page with the full registry.
    """
    if not rows:
        return "No models found."
    widths = {
        col: max(len(col.title().replace("_", " ")), *(len(r[col]) for r in rows))
        for col in ("name", "version", "status", "project", "owner", "updated_at")
    }
    header = (
        f"{'Name':<{widths['name']}}  "
        f"{'Version':<{widths['version']}}  "
        f"{'Status':<{widths['status']}}  "
        f"{'Project':<{widths['project']}}  "
        f"{'Owner':<{widths['owner']}}  "
        f"{'Updated':<{widths['updated_at']}}"
    )
    sep = "-" * len(header)
    lines = ["Model Registry", header, sep]
    for r in rows:
        lines.append(
            f"{r['name']:<{widths['name']}}  "
            f"{r['version']:<{widths['version']}}  "
            f"{r['status']:<{widths['status']}}  "
            f"{r['project']:<{widths['project']}}  "
            f"{r['owner']:<{widths['owner']}}  "
            f"{r['updated_at']:<{widths['updated_at']}}"
        )
    lines.append(sep)
    if total > len(rows):
        lines.append(f"Showing {len(rows)} of {total}")
    else:
        lines.append(f"Total: {len(rows)}")
    return "\n".join(lines)


def _resolve_model_name(
    ctx: Context,
    name: str,
    *,
    workspace_id: Optional[str] = None,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    pick: Optional[int] = None,
) -> str:
    def _lister():
        session = get_web_session()
        items, _ = browser_api_module.list_models(
            workspace_id=workspace_id,
            page=1,
            page_size=100,
            keyword=name,
            project_ids=[project_id] if project_id else None,
            user_id=user_id,
            session=session,
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
    )


@click.command("list")
@click.option("--workspace", default=None, help="Workspace name")
@click.option("--project", default=None, help="Project name filter")
@click.option("--keyword", default=None, help="Server-side model name/description search")
@click.option("--page", type=int, default=1, show_default=True)
@click.option("--page-size", type=int, default=-1, show_default=True, help="-1 = fetch all")
@pass_context
def list_model(
    ctx: Context,
    workspace: Optional[str],
    project: Optional[str],
    keyword: Optional[str],
    page: int,
    page_size: int,
) -> None:
    """List models in the current (or given) workspace."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        resolved_workspace = _resolve_workspace_id(config, workspace)
        session = get_web_session()
        project_id = _resolve_project_id(
            config, project, workspace_id=resolved_workspace, session=session
        )
        user_id = _current_user_id(session)
        items, total = browser_api_module.list_models(
            workspace_id=resolved_workspace,
            page=page,
            page_size=page_size,
            keyword=keyword,
            project_ids=[project_id] if project_id else None,
            user_id=user_id,
            session=session,
        )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"total": total, "items": [m.raw if m.raw else m.__dict__ for m in items]}
                )
            )
            return

        rows = [
            {
                "name": scrub_raw_ids(m.name or "-"),
                "version": scrub_raw_ids(f"V{m.latest_version}" if m.latest_version else "-"),
                "status": scrub_raw_ids(_status_label(m.status)),
                "project": scrub_raw_ids(m.project_name or "-"),
                "owner": scrub_raw_ids(m.user_name or "-"),
                "updated_at": scrub_raw_ids(format_epoch(m.updated_at) if m.updated_at else "-"),
            }
            for m in items
        ]
        click.echo(_format_model_rows(rows, total=int(total) if total is not None else len(rows)))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("status")
@click.argument("name")
@click.option("--workspace", default=None, help="Workspace name")
@click.option("--project", default=None, help="Project name filter")
@click.option("--pick", type=int, default=None, help="Pick Nth duplicate name (1-indexed)")
@pass_context
def status_model(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    project: Optional[str],
    pick: Optional[int],
) -> None:
    """Show detail of a specific model by name."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(config, workspace)
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
        )
        data = browser_api_module.get_model_detail(
            model_id=model_id, session=session, workspace_id=workspace_id
        )
        version_data = browser_api_module.list_model_version_records(
            model_id=model_id, session=session, workspace_id=workspace_id
        )

        if ctx.json_output:
            click.echo(json_formatter.format_json({"detail": data, "versions": version_data}))
            return

        model_payload = data.get("model")
        inner: dict[str, Any] = model_payload if isinstance(model_payload, dict) else data
        version_items = version_data.get("list") if isinstance(version_data, dict) else []
        latest_version: dict[str, Any] = {}
        if isinstance(version_items, list) and version_items:
            def _version_key(item: dict[str, Any]) -> int:
                model_obj = item.get("model")
                inner_obj: dict[str, Any] = (
                    model_obj if isinstance(model_obj, dict) else item
                )
                try:
                    return int(inner_obj.get("version") or 0)
                except (TypeError, ValueError):
                    return 0

            latest_item = max(
                [item for item in version_items if isinstance(item, dict)],
                key=_version_key,
                default={},
            )
            payload = latest_item.get("model") if isinstance(latest_item, dict) else None
            latest_version = payload if isinstance(payload, dict) else latest_item
        click.echo("Model")
        click.echo(f"Name:        {scrub_raw_ids(inner.get('name', 'N/A'))}")
        click.echo(
            f"Status:      {scrub_raw_ids(_status_label(latest_version.get('status', inner.get('status'))))}"
        )
        version_value = latest_version.get("version") or inner.get("version")
        if version_value:
            click.echo(f"Version:     V{version_value}")
        click.echo(f"Description: {scrub_raw_ids(inner.get('description', '') or '(none)')}")
        if inner.get("model_type"):
            click.echo(f"Type:        {scrub_raw_ids(_join_values(inner.get('model_type')))}")
        if inner.get("tags"):
            click.echo(f"Tags:        {scrub_raw_ids(_join_values(inner.get('tags')))}")
        vllm_ready = latest_version.get("is_vllm_compatible", inner.get("is_vllm_compatible"))
        click.echo(f"vLLM-ready:  {'yes' if vllm_ready else 'no'}")
        click.echo(f"Published:   {'yes' if inner.get('has_published') else 'no'}")
        if latest_version.get("model_path"):
            click.echo(f"Path:        {scrub_raw_ids(latest_version.get('model_path'))}")
        if latest_version.get("model_source_path"):
            click.echo(f"Source:      {scrub_raw_ids(latest_version.get('model_source_path'))}")
        if data.get("project_name"):
            click.echo(f"Project:     {scrub_raw_ids(data.get('project_name'))}")
        if data.get("user_name"):
            click.echo(f"Owner:       {scrub_raw_ids(data.get('user_name'))}")
        if inner.get("created_at"):
            click.echo(f"Created:     {format_epoch(inner.get('created_at'))}")
        if inner.get("updated_at"):
            click.echo(f"Updated:     {format_epoch(inner.get('updated_at'))}")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("versions")
@click.argument("name")
@click.option("--workspace", default=None, help="Workspace name")
@click.option("--project", default=None, help="Project name filter")
@click.option("--pick", type=int, default=None, help="Pick Nth duplicate name (1-indexed)")
@pass_context
def versions_model(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    project: Optional[str],
    pick: Optional[int],
) -> None:
    """List all versions of a model by name."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(config, workspace)
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
        )
        data = browser_api_module.list_model_version_records(
            model_id=model_id, session=session, workspace_id=workspace_id
        )

        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return

        items = data.get("list") if isinstance(data, dict) else None
        if not items:
            click.echo(f"No versions for model {scrub_raw_ids(name)}.")
            return

        click.echo(
            f"Versions for {scrub_raw_ids(name)}  (total={data.get('total', len(items))}, "
            f"next={scrub_raw_ids(data.get('next_version', '?'))})"
        )
        for i, item in enumerate(items, 1):
            model_payload = item.get("model") if isinstance(item, dict) else None
            inner = model_payload if isinstance(model_payload, dict) else item
            version = inner.get("version") or inner.get("model_version") or "?"
            size = (
                inner.get("model_size_gi")
                or inner.get("model_size_gb")
                or inner.get("size")
                or ""
            )
            path = inner.get("model_path") or ""
            source_path = inner.get("model_source_path") or ""
            vllm = "vLLM" if inner.get("is_vllm_compatible") else ""
            status = _status_label(inner.get("status") or item.get("status"))
            bits = [f"v{version}"]
            if size:
                bits.append(_format_size_gi(size))
            if status and status != "-":
                bits.append(status)
            if vllm:
                bits.append(vllm)
            running = item.get("running_infrence_serving")
            if running not in (None, ""):
                bits.append(f"running_servings={running}")
            if path:
                bits.append(f"path={scrub_raw_ids(path)}")
            if source_path:
                bits.append(f"source={scrub_raw_ids(source_path)}")
            click.echo(f"  [{i}] " + "  ".join(scrub_raw_ids(b) for b in bits))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("register")
@click.option("--name", "-n", required=True, help="Model name")
@click.option("--source-path", required=True, help="Platform-visible model directory")
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
@click.option("--source-type", type=int, default=1, show_default=True)
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
    source_type: int,
) -> None:
    """Register a platform-visible model directory in the model repository."""
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

        data = browser_api_module.create_model(
            name=name,
            project_id=project_id,
            workspace_id=workspace_id,
            model_source_path=source_path,
            model_type=model_type,
            tags=tags,
            description=description,
            model_source_type=source_type,
            session=session,
        )

        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return

        click.echo(f"OK Model registered: {scrub_raw_ids(name)}")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = ["list_model", "register_model", "status_model", "versions_model"]
