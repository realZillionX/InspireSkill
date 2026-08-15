"""Quota discovery commands for workload command groups."""

from __future__ import annotations

from typing import Any

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.table import render_table
from inspire.cli.utils.collection_output import (
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workspaces import resolve_workspace_query_scope, workspace_name_map
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session
from inspire.cli.utils.quota_cache import (
    SCHEDULE_TYPE_BY_WORKLOAD,
    CachedPricesLoader,
    group_supports_workload,
)
from inspire.cli.utils.quota_resolver import (
    PRIORITY_LEVELS_ANY_DISPLAY,
    PRIORITY_LEVELS_UNKNOWN_DISPLAY,
    QuotaMatchError,
    allowed_priority_levels_for,
    describe_priority_levels,
    load_quota_priority_levels,
    validate_compute_group_name,
)


def _group_id(group: dict[str, Any]) -> str:
    return str(group.get("logic_compute_group_id") or group.get("id") or "").strip()


def _group_name(group: dict[str, Any], fallback: str) -> str:
    return str(group.get("name") or group.get("logic_compute_group_name") or fallback).strip()


def _extract_gpu_type(price: dict[str, Any]) -> str:
    gpu_info_payload = price.get("gpu_info")
    gpu_info: dict[str, Any] = gpu_info_payload if isinstance(gpu_info_payload, dict) else {}
    return str(
        gpu_info.get("gpu_type_display")
        or gpu_info.get("gpu_type")
        or gpu_info.get("brand_name")
        or price.get("gpu_type")
        or ("CPU" if int(price.get("gpu_count") or 0) == 0 else "")
    ).strip()


def _extract_memory_gib(price: dict[str, Any]) -> int:
    value = (
        price.get("memory_size_gib") or price.get("memory_size") or price.get("memory_size_gb") or 0
    )
    try:
        return int(value)
    except Exception:
        return 0


def _query_workspace_quotas(
    *,
    session,  # noqa: ANN001
    workspace_id: str,
    workspace_name: str,
    workload: str,
    group_filter: str,
    include_empty: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_rows: set[tuple[str, int, int, int, str, str]] = set()
    public_workspace_name = scrub_raw_ids(workspace_name)
    groups = browser_api_module.list_notebook_compute_groups(
        workspace_id=workspace_id,
        session=session,
    )
    load_prices = CachedPricesLoader(
        session=session,
        workspace_id=workspace_id,
        schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD[workload],
    )
    # One request for the whole workspace, and only once a row needs it: a
    # `--workspace all` sweep with a `--group` filter would otherwise pay for
    # every workspace whose groups it then skips.
    menu: list[dict[str, tuple[str, ...]] | None] = []

    def priority_levels() -> dict[str, tuple[str, ...]] | None:
        if not menu:
            menu.append(
                load_quota_priority_levels(
                    workspace_id=workspace_id,
                    session=session,
                    workload=workload,
                )
            )
        return menu[0]

    for item in groups:
        logic_compute_group_id = _group_id(item)
        if not logic_compute_group_id:
            continue
        # A group that does not run this workload has no valid quota row for
        # it, however many rows its price table returns.
        if not group_supports_workload(item, workload):
            continue
        compute_group_name = _group_name(item, fallback="")
        if not compute_group_name:
            continue
        if group_filter and group_filter not in compute_group_name.lower():
            continue

        prices = load_prices(logic_compute_group_id)
        if not prices:
            if include_empty:
                rows.append(
                    {
                        "workspace": public_workspace_name,
                        "compute_group": scrub_raw_ids(compute_group_name),
                        "gpu_type": "",
                        "quota": "",
                        "priority": "",
                        "allowed_priority_levels": None,
                    }
                )
            continue

        for price in prices:
            cpu_count = int(price.get("cpu_count") or 0)
            memory_size_gib = _extract_memory_gib(price)
            gpu_count = int(price.get("gpu_count") or 0)
            gpu_type = _extract_gpu_type(price)
            quota_id = str(price.get("quota_id") or price.get("spec_id") or "").strip()
            levels = allowed_priority_levels_for(
                priority_levels(), quota_id, workload=workload
            )
            priority = describe_priority_levels(levels)
            # Two rows that differ only in what priorities they accept are two
            # different offers, so the restriction is part of the identity.
            key = (
                compute_group_name,
                gpu_count,
                cpu_count,
                memory_size_gib,
                gpu_type,
                priority,
            )
            if key in seen_rows:
                continue
            seen_rows.add(key)
            rows.append(
                {
                    "workspace": public_workspace_name,
                    "compute_group": scrub_raw_ids(compute_group_name),
                    "gpu_type": scrub_raw_ids(gpu_type),
                    "quota": f"{gpu_count},{cpu_count},{memory_size_gib}",
                    "priority": priority,
                    # `null` is "the platform did not answer", `[]` is "no
                    # restriction"; a consumer that collapses them is wrong.
                    "allowed_priority_levels": list(levels) if levels is not None else None,
                }
            )
    return rows


def _sort_rows(rows: list[dict[str, Any]]) -> None:
    rows.sort(
        key=lambda r: (
            str(r.get("workspace", "")),
            str(r.get("compute_group", "")),
            str(r.get("gpu_type", "")),
            str(r.get("quota", "")),
        )
    )


def make_quota_command(workload: str) -> click.Command:
    """Build ``inspire <workload> quota``."""

    @click.command("quota")
    @click.option(
        "--workspace",
        required=True,
        metavar="NAME|all",
        help="Workspace name or 'all'.",
    )
    @click.option(
        "--group",
        default=None,
        metavar="NAME",
        help=(
            "Filter by compute group name keyword/substring; full name is not "
            "required. Use this to find the exact compute group name required by "
            "create/profile --group."
        ),
    )
    @click.option(
        "--include-empty",
        is_flag=True,
        help="Include compute groups that return no quota rows for this workload.",
    )
    @click.option(
        "--limit",
        "-n",
        type=click.IntRange(min=1),
        default=None,
        help="Maximum quota rows to display (default: 20).",
    )
    @click.option("--all", "show_all", is_flag=True, help="Show every matching quota row.")
    @pass_context
    def quota_cmd(
        ctx: Context,
        workspace: str,
        group: str | None,
        include_empty: bool,
        limit: int | None,
        show_all: bool,
    ) -> None:
        """List valid ``--quota gpu,cpu,mem`` triples for this workload.

        The Priority column is the workspace's own statement about which task
        priorities each row accepts: 'any', 'low' (only --priority 1), or
        'unknown' when the platform did not answer.
        """
        try:
            effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
        except ValueError as e:
            _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
            return

        try:
            config, _ = Config.from_files_and_env(require_credentials=False)
            session = get_web_session()
            workspace_ids, _ = resolve_workspace_query_scope(
                workspace=workspace,
                session=session,
            )
            workspace_names = workspace_name_map(session)

            group_filter = (
                validate_compute_group_name(group).casefold() if group is not None else ""
            )
            rows: list[dict[str, Any]] = []
            display_names = [
                workspace_names.get(wid) or "(workspace name unavailable)"
                for wid in workspace_ids
            ]
            for workspace_id, workspace_name in zip(workspace_ids, display_names):
                rows.extend(
                    _query_workspace_quotas(
                        session=session,
                        workspace_id=workspace_id,
                        workspace_name=workspace_name,
                        workload=workload,
                        group_filter=group_filter,
                        include_empty=include_empty,
                    )
                )
            _sort_rows(rows)
            page = bound_collection(rows, limit=effective_limit)
            rows = page.items

            if ctx.json_output:
                click.echo(
                    json_formatter.format_json(
                        {
                            "items": rows,
                            **page.metadata(),
                        }
                    )
                )
                return

            if not rows:
                click.echo("No quota rows found.")
                return

            multi_ws = len({r.get("workspace") for r in rows}) > 1
            table_rows: list[tuple[Any, ...]]
            if multi_ws:
                headers: tuple[str, ...] = (
                    "Workspace",
                    "Compute Group",
                    "GPU Type",
                    "Quota",
                    "Priority",
                )
                widths = [18, 28, 14, 14, 9]
                table_rows = [
                    (
                        row["workspace"],
                        row["compute_group"],
                        row["gpu_type"] or "CPU",
                        row["quota"] or "-",
                        row["priority"] or "-",
                    )
                    for row in rows
                ]
            else:
                headers = ("Compute Group", "GPU Type", "Quota", "Priority")
                widths = [28, 14, 14, 9]
                table_rows = [
                    (
                        row["compute_group"],
                        row["gpu_type"] or "CPU",
                        row["quota"] or "-",
                        row["priority"] or "-",
                    )
                    for row in rows
                ]

            click.echo("\n".join(render_table(headers, table_rows, widths)))
            notice = truncation_notice(page)
            if notice:
                click.echo(notice)
            shown = {str(row.get("priority") or "") for row in rows}
            if PRIORITY_LEVELS_UNKNOWN_DISPLAY in shown:
                click.echo(
                    f"Priority '{PRIORITY_LEVELS_UNKNOWN_DISPLAY}': the workspace's "
                    "scheduling record could not be read, so nothing was ruled in or out "
                    "for those rows."
                )
            if shown - {PRIORITY_LEVELS_ANY_DISPLAY, PRIORITY_LEVELS_UNKNOWN_DISPLAY, ""}:
                click.echo(
                    "Priority is the task priority the platform publishes for that quota "
                    f"row; '{PRIORITY_LEVELS_ANY_DISPLAY}' means unrestricted, 'low' means "
                    "only --priority 1 (preemptible) will be accepted."
                )
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
        except QuotaMatchError as e:
            _handle_error(ctx, "ValidationError", scrub_raw_ids(e), EXIT_VALIDATION_ERROR)
        except SessionExpiredError as e:
            _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
        except ValueError as e:
            _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)
        except Exception as e:
            _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)

    return quota_cmd


__all__ = ["make_quota_command"]
