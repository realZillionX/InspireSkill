"""Cached resource-name mapping commands."""

from __future__ import annotations

import sqlite3
import time
from typing import Sequence

import click

from inspire.cli.context import (
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    Context,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.commands.notebook.gpu_model import (
    clear_gpu_model_cache,
    gpu_model_cache_status,
)
from inspire.cli.utils.errors import exit_with_error, require_confirmation
from inspire.cli.utils.id_resolver import reject_id_at_boundary
from inspire.cli.utils.notebook_cli import WEB_AUTH_HINT, require_web_session
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.resource_index import (
    DEFAULT_TTL_SECONDS,
    ResourceIndex,
    ResourceIndexDatabaseError,
    ScopeStatus,
)
from inspire.cli.utils.resource_index_refresh import (
    RESOURCE_TYPES,
    RefreshResult,
    refresh_resource_index,
)
from inspire.platform.web.session.models import WebSession

# The probed notebook GPU models are not name mappings and do not live in the
# index, but they are cache all the same: one more kind to show and to clear.
GPU_MODEL_RESOURCE = "notebook-gpu"
CACHE_RESOURCES: tuple[str, ...] = (*RESOURCE_TYPES, GPU_MODEL_RESOURCE)


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
        hint="Retry after other Inspire commands finish.",
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
        str(workspace_id): scrub_raw_ids(str(name))
        for workspace_id, name in names.items()
        if workspace_id and name
    }


def _public_error(value: object) -> str:
    return json_formatter.sanitize_text(value, redact_paths=True)


def _reports_nothing(row: dict[str, object]) -> bool:
    """Whether a status row carries neither names nor a problem worth reading.

    Only a scope nothing has ever touched is noise. A scope that *was*
    refreshed and still holds nothing is the opposite of noise -- see the
    ``empty`` branch in :func:`_scope_state` for what it once cost.
    """
    return (
        not row.get("cached_names")
        and not row.get("errors")
        and row.get("state") == "empty"
        and row.get("updated") == "never"
    )


def _touched_at(status: ScopeStatus) -> float:
    """When this scope last learned anything, complete scan or not.

    A targeted name lookup is intentionally partial, while an explicit cache
    refresh is complete. Both are useful observations, so status reports the
    newer timestamp instead of calling a recently resolved scope ``never``.
    """
    return max(status.last_full_refresh_at, status.last_refresh_at)


def _status_payload(
    index: ResourceIndex,
    *,
    resources: Sequence[str] = (),
) -> dict[str, object]:
    selected = {str(resource or "").strip().lower() for resource in resources}
    selected.discard("")
    names = _workspace_name_map()
    now = time.time()
    scopes: list[dict[str, object]] = []
    statuses = [
        status
        for status in index.list_scope_status()
        if not selected or status.resource_type in selected
    ]
    for status in statuses:
        row: dict[str, object] = {
            "resource": status.resource_type,
            "cached_names": status.active_count,
            "updated": _age(_touched_at(status), now=now),
        }
        workspace_name = names.get(status.workspace_id, "")
        if workspace_name:
            row["workspace"] = workspace_name
        if status.last_error:
            row["state"] = "error"
            row["error"] = _public_error(status.last_error)
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
    items: list[dict[str, object]] = []
    for resource, rows in sorted(by_resource.items()):
        state = max(
            (str(row.get("state") or "empty") for row in rows),
            key=lambda value: state_rank.get(value, 1),
        )
        refresh_times = [
            touched
            for status in statuses
            if status.resource_type == resource
            and (touched := _touched_at(status)) > 0
        ]
        item_counts = [
            value
            for row in rows
            if isinstance((value := row.get("cached_names")), int)
        ]
        cached_names = sum(item_counts)
        if state == "ready" and not cached_names:
            # Refreshed, in date, and holding nothing anywhere. `ready` read as
            # the healthiest state there is, which is how a quota catalog that
            # answered nothing for every compute group -- and so refused every
            # `--quota` the platform would have taken -- sat in plain sight.
            #
            # Per workspace this would be normal (a workspace really can have
            # no notebooks), so the verdict is only drawn across the whole
            # resource: nothing cached anywhere, after a refresh that claimed
            # to have run.
            state = "empty"
        summary: dict[str, object] = {
            "resource": resource,
            "cached_names": cached_names,
            "state": state,
            "updated": _age(min(refresh_times), now=now) if refresh_times else "never",
        }
        workspace_count = sum(bool(row.get("workspace")) for row in rows)
        if workspace_count:
            summary["workspaces"] = workspace_count
        error_count = sum(bool(row.get("error")) for row in rows)
        if error_count:
            summary["errors"] = error_count
            failures: list[dict[str, str]] = []
            for row in rows:
                error = str(row.get("error") or "")
                if not error:
                    continue
                failure = {"error": error}
                workspace = str(row.get("workspace") or "")
                if workspace:
                    failure["workspace"] = workspace
                failures.append(failure)
            summary["failures"] = failures
        items.append(summary)

    if not selected or GPU_MODEL_RESOURCE in selected:
        gpu_count, gpu_observed_at = gpu_model_cache_status()
        items.append(
            {
                "resource": GPU_MODEL_RESOURCE,
                "cached_names": gpu_count,
                "state": "ready" if gpu_count else "empty",
                "updated": _age(gpu_observed_at, now=now),
            }
        )

    # An explicitly named kind always gets a row: "not cached yet" is an answer
    # to "what is the state of X", and silence is not.
    reported = {str(item["resource"]) for item in items}
    for resource in sorted(selected - reported):
        items.append(
            {
                "resource": resource,
                "cached_names": 0,
                "state": "empty",
                "updated": "never",
            }
        )
    items.sort(key=lambda item: str(item["resource"]))

    return {"items": items}


def _refresh_payload(
    results: Sequence[RefreshResult],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "refreshed": sum(result.outcome == "refreshed" for result in results),
        "fresh": sum(result.outcome == "fresh" for result in results),
        "stale": sum(result.outcome == "stale" for result in results),
        "busy": sum(result.outcome == "busy" for result in results),
        "partial": sum(result.outcome == "partial" for result in results),
        "errors": sum(result.outcome == "error" for result in results),
        "names_cached": sum(
            result.item_count
            for result in results
            if result.outcome in {"refreshed", "partial"}
        ),
    }
    failures = [
        {
            **({"workspace": scrub_raw_ids(result.workspace_name)} if result.workspace_name else {}),
            "resource": result.resource_type,
            "error": _public_error(result.error),
        }
        for result in results
        if result.outcome == "error"
    ]
    if failures:
        payload["failures"] = failures
    # A partial scope is not a failure -- the rows it did read are cached and
    # the ones it could not read are still the previously cached ones -- but it
    # is not the whole catalog either, and silence would read as "complete".
    incomplete = [
        {
            **({"workspace": scrub_raw_ids(result.workspace_name)} if result.workspace_name else {}),
            "resource": result.resource_type,
            "reason": _public_error(result.error),
        }
        for result in results
        if result.outcome == "partial"
    ]
    if incomplete:
        payload["incomplete"] = incomplete
    return payload


@click.group("cache")
def cache() -> None:
    """Manage cached resource-name mappings."""


@cache.command("refresh")
@click.option(
    "--resource",
    "resources",
    type=click.Choice(RESOURCE_TYPES, case_sensitive=False),
    multiple=True,
    metavar="RESOURCE",
    help="Refresh only this resource kind. Repeat to select several.",
)
@click.option(
    "--workspace",
    "workspaces",
    multiple=True,
    metavar="NAME|all",
    help="Refresh only this workspace name. Repeat or use 'all'.",
)
@click.option("--name", default="", metavar="NAME", help="Refresh one exact resource name.")
@click.option(
    "--full",
    is_flag=True,
    help="Force a complete refresh even when cached scopes are still fresh.",
)
@pass_context
def refresh_cache(
    ctx: Context,
    resources: tuple[str, ...],
    workspaces: tuple[str, ...],
    name: str,
    full: bool,
) -> None:
    """Refresh one named slice of the cache.

    Say what to refresh. Every kind in every workspace is a few hundred
    requests and reads catalogs that only move when an admin edits them, so
    there is no bare form of this command — and normally nothing to run at
    all. Normal name resolution is read-through/write-through: a fresh hit is
    local, while an expired entry or miss refreshes only the requested name.

    Reach for it when you know something changed under the cache: an admin
    edited a compute group's specs, or an image was deleted from the web UI.

    \b
    Examples:
        inspire cache refresh --resource notebook --workspace 分布式训练空间
        inspire cache refresh --resource quota-job --workspace CPU资源空间 --full
        inspire cache refresh --resource image --name pytorch:2.1
    """
    if not (resources or workspaces or name):
        exit_with_error(
            ctx,
            "ValidationError",
            "Say which part of the cache to refresh.",
            EXIT_VALIDATION_ERROR,
            hint=(
                "Narrow it with --resource <kind> and/or --workspace <name>, "
                "e.g. `inspire cache refresh --resource notebook --workspace "
                "<name>`. Run `inspire cache status` first to see which scope "
                "is actually stale."
            ),
        )
    selected = tuple(resource.lower() for resource in resources) or RESOURCE_TYPES
    exact_name = str(name or "").strip()
    validated_workspaces = tuple(
        reject_id_at_boundary(
            ctx,
            workspace,
            resource_type="workspace",
            list_command="inspire account context",
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

    index = _index_or_exit(ctx)
    session = require_web_session(ctx, hint=WEB_AUTH_HINT)
    try:
        summary = refresh_resource_index(
            session=session,
            index=index,
            resource_types=selected,
            workspace_names=validated_workspaces or None,
            exact_name=exact_name,
            force=bool(full or exact_name or resources or workspaces),
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

    payload = _refresh_payload(summary.results)
    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                payload,
                success=not bool(summary.error_count),
            )
        )
    else:
        parts = [
            f"{payload['refreshed']} refreshed",
            f"{payload['names_cached']} names cached",
        ]
        for key, label in (
            ("fresh", "fresh"),
            ("stale", "superseded"),
            ("busy", "busy"),
            ("partial", "incomplete"),
            ("errors", "errors"),
        ):
            if payload[key]:
                parts.append(f"{payload[key]} {label}")
        click.echo(", ".join(parts) + ".")
        for result in summary.results:
            if result.outcome not in {"error", "partial"}:
                continue
            label = result.resource_type
            if result.workspace_name:
                label += f" @ {scrub_raw_ids(result.workspace_name)}"
            prefix = "Error" if result.outcome == "error" else "Incomplete"
            click.echo(f"{prefix}: {label}: {_public_error(result.error)}", err=True)

    if summary.error_count:
        raise SystemExit(EXIT_API_ERROR)


@cache.command("status")
@click.option(
    "--resource",
    "resources",
    type=click.Choice(CACHE_RESOURCES, case_sensitive=False),
    multiple=True,
    metavar="RESOURCE",
    help="Report only this cache kind. Repeat to select several. Default: all of them.",
)
@pass_context
def cache_status(ctx: Context, resources: tuple[str, ...]) -> None:
    """Show cache freshness and name counts.

    A kind named with --resource always gets a row, reported as empty when
    nothing is cached for it yet.

    \b
    Examples:
        inspire cache status
        inspire cache status --resource notebook
        inspire cache status --resource notebook-gpu --resource quota-notebook
    """
    try:
        payload = _status_payload(_index_or_exit(ctx), resources=resources)
    except (OSError, sqlite3.Error, ResourceIndexDatabaseError):
        _exit_cache_database_error(ctx)
        raise RuntimeError("unreachable")
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
        return

    items = payload["items"]
    rows = [row for row in items if isinstance(row, dict)] if isinstance(items, list) else []
    # A whole-cache view of nothing reads better as one sentence than as a
    # column of zeroes. A --resource view still gets its rows: "empty" is the
    # answer it asked for.
    if not resources and all(_reports_nothing(row) for row in rows):
        click.echo("Resource name cache is empty.")
        return
    for row in rows:
        label = str(row["resource"])
        if row.get("workspaces"):
            label += f" ({row['workspaces']} workspaces)"
        click.echo(
            f"{label}: {row['cached_names']} names, {row['state']}, {row['updated']}"
        )
        failures = row.get("failures")
        if not isinstance(failures, list):
            continue
        for failure in failures:
            if not isinstance(failure, dict):
                continue
            workspace = str(failure.get("workspace") or "")
            suffix = f" @ {workspace}" if workspace else ""
            click.echo(
                f"Error{suffix}: {_public_error(failure.get('error'))}",
                err=True,
            )


@cache.command("clear")
@click.option(
    "--resource",
    "resources",
    type=click.Choice(CACHE_RESOURCES, case_sensitive=False),
    multiple=True,
    metavar="RESOURCE",
    help="Clear only this cache kind. Repeat to select several. Default: all of them.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@pass_context
def clear_cache(ctx: Context, resources: tuple[str, ...], yes: bool) -> None:
    """Clear local caches, all of them or one kind at a time.

    \b
    Examples:
        inspire cache clear --yes
        inspire cache clear --resource notebook --yes
        inspire cache clear --resource notebook-gpu --resource quota-notebook --yes
    """
    selected = tuple(dict.fromkeys(resource.lower() for resource in resources))
    scope_label = ", ".join(selected) if selected else "every local cache"
    require_confirmation(
        ctx,
        yes=yes,
        prompt=f"Clear {scope_label}?",
        message="Cache clearing requires confirmation.",
        hint="Pass --yes to confirm clearing the cache.",
    )

    index_types = [
        resource for resource in selected if resource != GPU_MODEL_RESOURCE
    ]
    cleared: dict[str, int] = {}
    if index_types or not selected:
        try:
            cleared["names"] = _index_or_exit(ctx).clear(index_types or None)
        except (OSError, sqlite3.Error, ResourceIndexDatabaseError):
            _exit_cache_database_error(ctx)
            raise RuntimeError("unreachable")
    if not selected or GPU_MODEL_RESOURCE in selected:
        cleared["gpu_models"] = clear_gpu_model_cache()

    payload: dict[str, object] = {"status": "cleared", **cleared}
    if selected:
        payload["resources"] = list(selected)
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
        return
    labels = {"names": "names", "gpu_models": "GPU models"}
    parts = [f"{count} {labels[key]}" for key, count in cleared.items()]
    click.echo(f"Cleared {scope_label}: " + ", ".join(parts) + ".")


__all__ = ["cache"]
