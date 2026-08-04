"""Commands for the disposable local resource-name index."""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Sequence

import click

from inspire.accounts import current_account
from inspire.cli.context import (
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    Context,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.id_resolver import reject_id_at_boundary
from inspire.cli.utils.notebook_cli import WEB_AUTH_HINT, require_web_session
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.resource_index import (
    DEFAULT_TTL_SECONDS,
    ResourceIndex,
    ResourceIndexDatabaseError,
)
from inspire.cli.utils.resource_index_refresh import (
    RESOURCE_TYPES,
    RefreshResult,
    refresh_resource_index,
)
from inspire.platform.web.session.models import WebSession


def _index_or_exit(ctx: Context, account: str | None = None) -> ResourceIndex:
    try:
        index = ResourceIndex.for_account(account)
    except (OSError, sqlite3.Error, ResourceIndexDatabaseError):
        _exit_cache_database_error(ctx)
        raise RuntimeError("unreachable")
    if index is None:
        exit_with_error(
            ctx,
            "ConfigError",
            "No active Inspire account.",
            EXIT_CONFIG_ERROR,
            hint="Run `inspire account use <name>` first.",
        )
        raise RuntimeError("unreachable")
    return index


def _exit_cache_database_error(ctx: Context) -> None:
    exit_with_error(
        ctx,
        "CacheError",
        "The local resource name cache is unavailable.",
        EXIT_API_ERROR,
        hint="Retry after other Inspire commands finish. Corrupt cache files are rebuilt automatically.",
    )


def _age(value: float, *, now: float | None = None) -> str:
    if value <= 0:
        return "never"
    seconds = max(0, int((time.time() if now is None else now) - value))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 60 * 60:
        return f"{seconds // 60}m ago"
    if seconds < 24 * 60 * 60:
        return f"{seconds // (60 * 60)}h ago"
    return f"{seconds // (24 * 60 * 60)}d ago"


def _workspace_name_map() -> dict[str, str]:
    session = WebSession.load(allow_expired=True)
    names = getattr(session, "all_workspace_names", None) if session else None
    if not isinstance(names, dict):
        return {}
    return {
        str(workspace_id): str(name)
        for workspace_id, name in names.items()
        if workspace_id and name
    }


def _status_payload(index: ResourceIndex, *, debug: bool = False) -> dict[str, object]:
    names = _workspace_name_map()
    now = time.time()
    scopes: list[dict[str, object]] = []
    statuses = index.list_scope_status()
    for status in statuses:
        row: dict[str, object] = {
            "resource": status.resource_type,
            "items": status.active_count,
            "updated": _age(status.last_full_refresh_at, now=now),
        }
        workspace_name = names.get(status.workspace_id, "")
        if workspace_name:
            row["workspace"] = workspace_name
        if status.last_error:
            row["state"] = "error"
            row["error"] = scrub_raw_ids(status.last_error)
        elif status.last_full_refresh_at <= 0 and status.last_refresh_at > 0:
            row["state"] = "partial"
        elif status.last_full_refresh_at <= 0:
            row["state"] = "empty"
        elif now - status.last_full_refresh_at >= DEFAULT_TTL_SECONDS.get(
            status.resource_type, 300
        ):
            row["state"] = "stale"
        else:
            row["state"] = "ready"
        scopes.append(row)

    by_resource: dict[str, list[dict[str, object]]] = {}
    for row in scopes:
        by_resource.setdefault(str(row["resource"]), []).append(row)

    state_rank = {"ready": 0, "empty": 1, "partial": 2, "stale": 3, "error": 4}
    resources: list[dict[str, object]] = []
    for resource, rows in sorted(by_resource.items()):
        state = max(
            (str(row.get("state") or "empty") for row in rows),
            key=lambda value: state_rank.get(value, 1),
        )
        refresh_times = [
            status.last_full_refresh_at
            for status in statuses
            if status.resource_type == resource and status.last_full_refresh_at > 0
        ]
        item_counts = [
            value
            for row in rows
            if isinstance((value := row.get("items")), int)
        ]
        summary: dict[str, object] = {
            "resource": resource,
            "items": sum(item_counts),
            "state": state,
            "updated": _age(min(refresh_times), now=now) if refresh_times else "never",
        }
        workspace_count = sum(bool(row.get("workspace")) for row in rows)
        if workspace_count:
            summary["workspaces"] = workspace_count
        error_count = sum(bool(row.get("error")) for row in rows)
        if error_count:
            summary["errors"] = error_count
        resources.append(summary)

    payload: dict[str, object] = {
        "account": current_account() or "",
        "resources": resources,
        "items": sum(
            value
            for scope in scopes
            if isinstance((value := scope.get("items")), int)
        ),
    }
    if debug:
        payload["scopes"] = scopes
    return payload


def _refresh_payload(
    results: Sequence[RefreshResult],
    *,
    debug: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "refreshed": sum(result.outcome == "refreshed" for result in results),
        "fresh": sum(result.outcome == "fresh" for result in results),
        "stale": sum(result.outcome == "stale" for result in results),
        "busy": sum(result.outcome == "busy" for result in results),
        "errors": sum(result.outcome == "error" for result in results),
        "items": sum(
            result.item_count for result in results if result.outcome == "refreshed"
        ),
    }
    failures = [
        result.to_payload() for result in results if result.outcome == "error"
    ]
    if failures:
        payload["failures"] = failures
    if debug:
        payload["scopes"] = [result.to_payload() for result in results]
    return payload


@click.group("cache")
def cache() -> None:
    """Manage the local resource-name acceleration index."""


@cache.command("refresh")
@click.option(
    "--resource",
    "resources",
    type=click.Choice(RESOURCE_TYPES, case_sensitive=False),
    multiple=True,
    help="Refresh only this resource kind. Repeat to select several.",
)
@click.option(
    "--workspace",
    "workspaces",
    multiple=True,
    metavar="NAME",
    help="Refresh only this workspace name. Repeat or use 'all'.",
)
@click.option("--name", default="", help="Refresh one exact resource name.")
@click.option(
    "--full",
    is_flag=True,
    help="Force a complete refresh even when cached scopes are still fresh.",
)
@click.option("--due", is_flag=True, hidden=True)
@click.option("--quiet", is_flag=True, hidden=True)
@click.option("--account", hidden=True)
@pass_context
def refresh_cache(
    ctx: Context,
    resources: tuple[str, ...],
    workspaces: tuple[str, ...],
    name: str,
    full: bool,
    due: bool,
    quiet: bool,
    account: str | None,
) -> None:
    """Refresh stale mappings, or force selected mappings with --full."""
    account = (
        str(account or "").strip()
        or os.environ.get("INSPIRE_RESOURCE_INDEX_REFRESH_ACCOUNT", "").strip()
        or None
    )
    selected = tuple(resource.lower() for resource in resources) or RESOURCE_TYPES
    exact_name = str(name or "").strip()
    validated_workspaces = tuple(
        reject_id_at_boundary(
            ctx,
            workspace,
            resource_type="workspace",
            list_command="inspire config context",
        )
        for workspace in workspaces
    )
    if exact_name:
        if len(selected) != 1:
            exit_with_error(
                ctx,
                "ValidationError",
                "--name requires exactly one --resource.",
                EXIT_VALIDATION_ERROR,
            )
        reject_id_at_boundary(
            ctx,
            exact_name,
            resource_type=selected[0],
            list_command=f"inspire {selected[0]} list",
        )

    index = _index_or_exit(ctx, account)
    session = require_web_session(ctx, hint=WEB_AUTH_HINT, account=account)
    force = bool(full or exact_name or resources or workspaces) and not due
    try:
        summary = refresh_resource_index(
            session=session,
            index=index,
            resource_types=selected,
            workspace_names=validated_workspaces or None,
            exact_name=exact_name,
            force=force,
        )
    except (OSError, sqlite3.Error, ResourceIndexDatabaseError):
        _exit_cache_database_error(ctx)
        raise RuntimeError("unreachable")
    except ValueError as exc:
        exit_with_error(
            ctx,
            "ValidationError",
            str(exc),
            EXIT_VALIDATION_ERROR,
        )

    if quiet:
        if summary.error_count:
            raise SystemExit(EXIT_API_ERROR)
        return

    payload = _refresh_payload(summary.results, debug=ctx.debug)
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
    else:
        click.echo(
            "Resource names: "
            f"{payload['refreshed']} refreshed, "
            f"{payload['fresh']} fresh, "
            f"{payload['stale']} changed during refresh, "
            f"{payload['items']} indexed"
            + (f", {payload['errors']} errors" if payload["errors"] else "")
            + "."
        )
        for result in summary.results:
            if result.outcome != "error":
                continue
            label = result.resource_type
            if result.workspace_name:
                label += f" @ {scrub_raw_ids(result.workspace_name)}"
            click.echo(f"Error: {label}: {scrub_raw_ids(result.error)}", err=True)

    if summary.error_count:
        raise SystemExit(EXIT_API_ERROR)


@cache.command("status")
@pass_context
def cache_status(ctx: Context) -> None:
    """Show compact cache freshness and item counts."""
    try:
        payload = _status_payload(_index_or_exit(ctx), debug=ctx.debug)
    except (OSError, sqlite3.Error, ResourceIndexDatabaseError):
        _exit_cache_database_error(ctx)
        raise RuntimeError("unreachable")
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
        return

    resources = payload["resources"]
    if not isinstance(resources, list) or not resources:
        click.echo("Resource name cache is empty.")
        return
    for row in resources:
        if not isinstance(row, dict):
            continue
        label = str(row["resource"])
        if row.get("workspaces"):
            label += f" ({row['workspaces']} workspaces)"
        click.echo(
            f"{label}: {row['items']} names, {row['state']}, {row['updated']}"
        )


@cache.command("clear")
@click.option("--yes", is_flag=True, help="Clear without prompting.")
@pass_context
def clear_cache(ctx: Context, yes: bool) -> None:
    """Clear the disposable local resource-name index."""
    index = _index_or_exit(ctx)
    if not yes and not click.confirm("Clear the local resource name cache?"):
        return
    try:
        index.clear()
    except (OSError, sqlite3.Error, ResourceIndexDatabaseError):
        _exit_cache_database_error(ctx)
        raise RuntimeError("unreachable")
    payload = {"cleared": True, "account": current_account() or ""}
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
    else:
        click.echo("Resource name cache cleared.")


__all__ = ["cache"]
