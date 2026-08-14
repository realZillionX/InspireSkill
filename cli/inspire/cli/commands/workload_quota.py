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
)
from inspire.cli.utils.quota_resolver import (
    QuotaMatchError,
    qz_scheduling_zone_hint_for_group_names,
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
    seen_rows: set[tuple[str, int, int, int, str]] = set()
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

    for item in groups:
        logic_compute_group_id = _group_id(item)
        if not logic_compute_group_id:
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
                    }
                )
            continue

        for price in prices:
            cpu_count = int(price.get("cpu_count") or 0)
            memory_size_gib = _extract_memory_gib(price)
            gpu_count = int(price.get("gpu_count") or 0)
            gpu_type = _extract_gpu_type(price)
            allowed_raw = price.get("allowed_priority_levels") or []
            allowed_priority = ",".join(
                str(level) for level in allowed_raw if str(level or "").strip()
            )
            key = (
                compute_group_name,
                gpu_count,
                cpu_count,
                memory_size_gib,
                gpu_type,
                allowed_priority,
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
                    "allowed_priority": allowed_priority,
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
        """List valid ``--quota gpu,cpu,mem`` triples for this workload."""
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

            def _allowed_display(value: str) -> str:
                # Empty means unrestricted; "low" means this quota exists in
                # this group only at low priority (``--priority 1``).
                return value if value else "any"

            if multi_ws:
                headers: tuple[str, ...] = (
                    "Workspace",
                    "Compute Group",
                    "GPU Type",
                    "Quota",
                    "Priority",
                )
                widths = [18, 24, 14, 14, 9]
                table_rows = [
                    (
                        row["workspace"],
                        row["compute_group"],
                        row["gpu_type"] or "CPU",
                        row["quota"] or "-",
                        _allowed_display(row["allowed_priority"]),
                    )
                    for row in rows
                ]
            else:
                headers = ("Compute Group", "GPU Type", "Quota", "Priority")
                widths = [24, 14, 14, 9]
                table_rows = [
                    (
                        row["compute_group"],
                        row["gpu_type"] or "CPU",
                        row["quota"] or "-",
                        _allowed_display(row["allowed_priority"]),
                    )
                    for row in rows
                ]

            click.echo("\n".join(render_table(headers, table_rows, widths)))
            notice = truncation_notice(page)
            if notice:
                click.echo(notice)
            qz_hint = qz_scheduling_zone_hint_for_group_names(
                row.get("compute_group") for row in rows
            )
            if qz_hint:
                click.echo(qz_hint)
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
