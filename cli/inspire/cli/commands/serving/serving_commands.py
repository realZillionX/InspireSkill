"""`inspire serving` subcommands."""

from __future__ import annotations

import json as _json
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
from inspire.cli.utils.id_resolver import resolve_by_name
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.openapi import InspireAPIError
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import get_web_session


def _resolve_serving_name(ctx: Context, name: str, *, pick: Optional[int] = None) -> str:
    """Resolve a serving name to its platform id (``sv-<uuid>``).

    Scope: ``my_serving=True`` (default) × session workspace, full page.
    """
    def _lister():
        session = get_web_session()
        items, _ = browser_api_module.list_servings(
            session=session, my_serving=True, page_size=10000
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


def _extract_data(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    return data if isinstance(data, dict) else result


def _resolve_workspace_id(config: Config, workspace: Optional[str]) -> Optional[str]:
    if workspace is None:
        return None
    return select_workspace_id(config, explicit_workspace_name=workspace)


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
        for col in ("id", "name", "status", "replicas", "created_at")
    }
    header = (
        f"{'ID':<{widths['id']}} {'Name':<{widths['name']}} "
        f"{'Status':<{widths['status']}} {'Replicas':<{widths['replicas']}} "
        f"{'Created':<{widths['created_at']}}"
    )
    sep = "-" * len(header)
    lines = ["Inference Servings", header, sep]
    for r in rows:
        lines.append(
            f"{r['id']:<{widths['id']}} "
            f"{r['name']:<{widths['name']}} "
            f"{r['status']:<{widths['status']}} "
            f"{r['replicas']:<{widths['replicas']}} "
            f"{r['created_at']:<{widths['created_at']}}"
        )
    lines.append(sep)
    if total > len(rows):
        lines.append(f"Showing {len(rows)} of {total}")
    else:
        lines.append(f"Total: {len(rows)}")
    return "\n".join(lines)


@click.command("list")
@click.option("--workspace", default=None, help="Workspace name (from [workspaces])")
@click.option(
    "-a",
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Show all servings in the workspace (default: only the current user's)",
)
@click.option("--page", type=int, default=1, show_default=True)
@click.option("--page-size", type=int, default=50, show_default=True)
@pass_context
def list_serving(
    ctx: Context,
    workspace: Optional[str],
    show_all: bool,
    page: int,
    page_size: int,
) -> None:
    """List inference servings in the current (or given) workspace."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        resolved_workspace = _resolve_workspace_id(config, workspace)

        session = get_web_session()
        items, total = browser_api_module.list_servings(
            workspace_id=resolved_workspace,
            my_serving=not show_all,
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
                "name": s.name or "-",
                "status": s.status or "-",
                "replicas": str(s.replicas or "-"),
                "created_at": s.created_at or "-",
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
@pass_context
def status_serving(ctx: Context, name: str) -> None:
    """Get detail of an inference serving (pass the serving name)."""
    try:
        config, _ = Config.from_files_and_env(require_target_dir=False)
        api = AuthManager.get_api(config)
        inference_serving_id = _resolve_serving_name(ctx, name)
        result = api.get_inference_serving_detail(inference_serving_id)
        data = _extract_data(result)

        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return

        click.echo("Inference Serving Status")
        click.echo(f"Name:     {data.get('name', 'N/A')}")
        click.echo(f"Status:   {data.get('status', 'N/A')}")
        if data.get("replicas") is not None:
            click.echo(f"Replicas: {data.get('replicas')}")
        if data.get("image"):
            click.echo(f"Image:    {data.get('image')}")
        if data.get("model_id"):
            click.echo(f"Model:    {data.get('model_id')} v{data.get('model_version', '?')}")
        if data.get("created_at"):
            click.echo(f"Created:  {data.get('created_at')}")
        if data.get("updated_at"):
            click.echo(f"Updated:  {data.get('updated_at')}")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except InspireAPIError as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("stop")
@click.argument("name")
@click.option(
    "--pick",
    type=int,
    default=None,
    help="Pick the Nth candidate (1-indexed) when the name is ambiguous.",
)
@pass_context
def stop_serving(ctx: Context, name: str, pick: Optional[int]) -> None:
    """Stop an inference serving (pass the serving name)."""
    try:
        config, _ = Config.from_files_and_env(require_target_dir=False)
        api = AuthManager.get_api(config)
        inference_serving_id = _resolve_serving_name(ctx, name, pick=pick)
        api.stop_inference_serving(inference_serving_id)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json({"name": name, "stopped": True})
            )
            return

        click.echo(human_formatter.format_success(f"Inference serving stopped: {name}"))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except InspireAPIError as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("configs")
@click.option("--workspace", default=None, help="Workspace name")
@pass_context
def configs_serving(
    ctx: Context, workspace: Optional[str],) -> None:
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

        configs = data.get("configs") if isinstance(data, dict) else None
        if not configs:
            click.echo("No inference-serving configs returned (workspace may be empty or not authorized).")
            return

        click.echo("Available Inference Serving Configs")
        if isinstance(configs, list):
            for i, c in enumerate(configs, 1):
                click.echo(f"[{i}] {_json.dumps(c, ensure_ascii=False)[:160]}")
        else:
            click.echo(_json.dumps(configs, ensure_ascii=False, indent=2))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = [
    "list_serving",
    "status_serving",
    "stop_serving",
    "configs_serving",
]
