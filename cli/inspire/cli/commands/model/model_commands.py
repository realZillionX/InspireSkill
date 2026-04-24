"""`inspire model` subcommands — registry browsing."""

from __future__ import annotations

from typing import Optional

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
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import get_web_session


def _resolve_workspace_id(config: Config, workspace: Optional[str]) -> Optional[str]:
    if workspace is None:
        return None
    return select_workspace_id(config, explicit_workspace_name=workspace)


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
        for col in ("model_id", "name", "latest", "vllm", "created_at")
    }
    header = (
        f"{'Model ID':<{widths['model_id']}} {'Name':<{widths['name']}} "
        f"{'Latest':<{widths['latest']}} {'vLLM':<{widths['vllm']}} "
        f"{'Created':<{widths['created_at']}}"
    )
    sep = "-" * len(header)
    lines = ["Model Registry", header, sep]
    for r in rows:
        lines.append(
            f"{r['model_id']:<{widths['model_id']}} "
            f"{r['name']:<{widths['name']}} "
            f"{r['latest']:<{widths['latest']}} "
            f"{r['vllm']:<{widths['vllm']}} "
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
@click.option("--page", type=int, default=1, show_default=True)
@click.option("--page-size", type=int, default=-1, show_default=True, help="-1 = fetch all")
@pass_context
def list_model(
    ctx: Context,
    workspace: Optional[str],
    page: int,
    page_size: int,
) -> None:
    """List models in the current (or given) workspace."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        resolved_workspace = _resolve_workspace_id(config, workspace)
        session = get_web_session()
        items, total = browser_api_module.list_models(
            workspace_id=resolved_workspace,
            page=page,
            page_size=page_size,
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
                "model_id": m.model_id or "-",
                "name": m.name or "-",
                "latest": m.latest_version or "-",
                "vllm": "yes" if m.is_vllm_compatible else "no",
                "created_at": format_epoch(m.created_at) if m.created_at else "-",
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
@click.argument("model_id")
@pass_context
def status_model(ctx: Context, model_id: str) -> None:
    """Show detail of a specific model."""
    try:
        session = get_web_session()
        data = browser_api_module.get_model_detail(model_id=model_id, session=session)

        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return

        inner = data.get("model") if isinstance(data.get("model"), dict) else data
        click.echo("Model")
        click.echo(f"Model ID:    {inner.get('model_id', model_id)}")
        click.echo(f"Name:        {inner.get('name', 'N/A')}")
        click.echo(f"Description: {inner.get('description', '') or '(none)'}")
        click.echo(f"vLLM-ready:  {'yes' if inner.get('is_vllm_compatible') else 'no'}")
        click.echo(f"Published:   {'yes' if inner.get('has_published') else 'no'}")
        if data.get("project_name"):
            click.echo(f"Project:     {data.get('project_name')}")
        if data.get("user_name"):
            click.echo(f"Owner:       {data.get('user_name')}")
        if inner.get("created_at"):
            click.echo(f"Created:     {format_epoch(inner.get('created_at'))}")

    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("versions")
@click.argument("model_id")
@pass_context
def versions_model(ctx: Context, model_id: str) -> None:
    """List all versions of a model."""
    try:
        session = get_web_session()
        data = browser_api_module.list_model_versions(model_id=model_id, session=session)

        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return

        items = data.get("list") if isinstance(data, dict) else None
        if not items:
            click.echo(f"No versions for model {model_id}.")
            return

        click.echo(f"Versions for {model_id}  (total={data.get('total', len(items))}, next={data.get('next_version', '?')})")
        for i, item in enumerate(items, 1):
            inner = item.get("model") if isinstance(item, dict) and isinstance(item.get("model"), dict) else item
            version = inner.get("version") or inner.get("model_version") or "?"
            size = inner.get("model_size_gb") or inner.get("size") or ""
            path = inner.get("model_path") or ""
            vllm = "vLLM" if inner.get("is_vllm_compatible") else ""
            bits = [f"v{version}"]
            if size:
                bits.append(f"{size} GB")
            if vllm:
                bits.append(vllm)
            if path:
                bits.append(f"path={path}")
            click.echo(f"  [{i}] " + "  ".join(str(b) for b in bits))

    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = ["list_model", "status_model", "versions_model"]
