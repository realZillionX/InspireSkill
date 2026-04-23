"""Config context command — surface the structured (non-scalar) parts of config.toml.

`inspire config show` walks the flat env-var-backed options (username, base_url,
timeout, ...). The structured pieces — the active [context], project / workspace
alias maps, compute groups, per-account catalogs — aren't reachable through that
output, so agents had no CLI way to answer questions like "what does the
workspace alias `gpu` resolve to?" or "what's the active project-id?" without
reading config.toml directly (which SKILL.md discourages).

This command fills that gap. Same two-format surface as `show`: human-readable
default + `--json` for programmatic consumers.
"""

from __future__ import annotations

from typing import Any

import click

from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import Config, ConfigError


def _workspace_role_aliases(cfg: Config) -> dict[str, str]:
    """The three role-slotted workspaces (cpu/gpu/internet)."""
    return {
        role: value
        for role, value in (
            ("cpu", cfg.workspace_cpu_id),
            ("gpu", cfg.workspace_gpu_id),
        )
        if value
    }


def _collect_context(cfg: Config) -> dict[str, Any]:
    from inspire.accounts import current_account, list_accounts

    active_account = current_account()
    accounts_listed = {name: "********" for name in list_accounts()}

    return {
        "active_context": {
            "account": active_account or cfg.username or None,
            "project_id": cfg.job_project_id,
            "workspace_id": cfg.job_workspace_id,
        },
        "project_aliases": dict(cfg.projects or {}),
        "workspace_aliases": dict(cfg.workspaces or {}),
        "workspace_roles": _workspace_role_aliases(cfg),
        "compute_groups": list(cfg.compute_groups or []),
        "accounts": accounts_listed,
        "account_metadata": {
            "shared_path_group": cfg.account_shared_path_group,
            "train_job_workdir": cfg.account_train_job_workdir,
        },
        "project_catalog": dict(cfg.project_catalog or {}),
        "project_shared_path_groups": dict(cfg.project_shared_path_groups or {}),
        "project_workdirs": dict(cfg.project_workdirs or {}),
    }


def _render_human(data: dict[str, Any]) -> None:
    active = data["active_context"]
    click.echo(click.style("Active context", bold=True))
    click.echo(f"  account      {active['account'] or '(not set)'}")
    click.echo(f"  project_id   {active['project_id'] or '(not set)'}")
    click.echo(f"  workspace_id {active['workspace_id'] or '(not set)'}")
    click.echo()

    project_aliases: dict[str, str] = data["project_aliases"]
    if project_aliases:
        click.echo(click.style(f"Project aliases ({len(project_aliases)})", bold=True))
        width = max(len(alias) for alias in project_aliases) + 2
        for alias, project_id in project_aliases.items():
            click.echo(f"  {alias.ljust(width)}→ {project_id}")
        click.echo()

    workspace_aliases: dict[str, str] = data["workspace_aliases"]
    if workspace_aliases:
        click.echo(click.style(f"Workspace aliases ({len(workspace_aliases)})", bold=True))
        width = max(len(alias) for alias in workspace_aliases) + 2
        for alias, ws_id in workspace_aliases.items():
            click.echo(f"  {alias.ljust(width)}→ {ws_id}")
        click.echo()

    workspace_roles: dict[str, str] = data["workspace_roles"]
    if workspace_roles:
        click.echo(click.style("Workspace roles", bold=True))
        for role, ws_id in workspace_roles.items():
            click.echo(f"  {role.ljust(10)}→ {ws_id}")
        click.echo()

    compute_groups: list[dict[str, Any]] = data["compute_groups"]
    if compute_groups:
        click.echo(click.style(f"Compute groups ({len(compute_groups)})", bold=True))
        for group in compute_groups:
            name = group.get("name") or group.get("id", "(unnamed)")
            gpu = group.get("gpu_type", "")
            loc = group.get("location", "")
            ws_ids = group.get("workspace_ids", [])
            bits: list[str] = []
            if gpu:
                bits.append(f"gpu={gpu}")
            if loc:
                bits.append(f"location={loc}")
            if ws_ids:
                bits.append(f"workspaces={len(ws_ids)}")
            suffix = f" ({', '.join(bits)})" if bits else ""
            click.echo(f"  {name}{suffix}")
            if group.get("id") and group.get("id") != name:
                click.echo(f"    id={group['id']}")
        click.echo()

    accounts: dict[str, str] = data["accounts"]
    if accounts:
        click.echo(click.style(f"Accounts ({len(accounts)})", bold=True))
        for username in accounts:
            click.echo(f"  {username}")
        click.echo()

    project_catalog: dict[str, dict[str, Any]] = data["project_catalog"]
    if project_catalog:
        click.echo(click.style(f"Project catalog ({len(project_catalog)})", bold=True))
        for project_id, entry in project_catalog.items():
            shared = entry.get("shared_path_group") or "-"
            workdir = entry.get("workdir") or "-"
            click.echo(f"  {project_id}")
            click.echo(f"    shared_path_group  {shared}")
            click.echo(f"    workdir            {workdir}")
        click.echo()

    acct_meta = data["account_metadata"]
    if any(acct_meta.values()):
        click.echo(click.style("Account metadata", bold=True))
        if acct_meta["shared_path_group"]:
            click.echo(f"  shared_path_group  {acct_meta['shared_path_group']}")
        if acct_meta["train_job_workdir"]:
            click.echo(f"  train_job_workdir  {acct_meta['train_job_workdir']}")


@click.command("context")
@click.option(
    "--json",
    "json_output_local",
    is_flag=True,
    help="Output as JSON (machine-readable). Equivalent to the top-level --json.",
)
@pass_context
def show_context(ctx: Context, json_output_local: bool) -> None:
    """Display the structured config layers (context, aliases, compute groups, accounts).

    Use this instead of reading `~/.config/inspire/config.toml` or
    `./.inspire/config.toml` directly — the CLI merges both layers and presents
    the active context, alias maps, compute groups, and per-account catalog in
    one place, with passwords elided.

    \b
    Examples:
        inspire config context
        inspire config context --json
    """
    effective_json = bool(ctx.json_output or json_output_local)

    try:
        cfg, _sources = Config.from_files_and_env(
            require_credentials=False,
            require_target_dir=False,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)
        return

    data = _collect_context(cfg)

    if effective_json:
        click.echo(json_formatter.format_json(data))
        return

    _render_human(data)
