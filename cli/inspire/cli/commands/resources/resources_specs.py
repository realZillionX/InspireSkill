"""Resources specs command (discover spec_id/quota_id by compute group)."""

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
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session


_USAGE_SCHEDULE_TYPES = {
    "auto": ("SCHEDULE_CONFIG_TYPE_HPC", "SCHEDULE_CONFIG_TYPE_DSW"),
    "notebook": ("SCHEDULE_CONFIG_TYPE_DSW",),
    "hpc": ("SCHEDULE_CONFIG_TYPE_HPC",),
    "ray": ("SCHEDULE_CONFIG_TYPE_RAY_JOB",),
    "all": (
        "SCHEDULE_CONFIG_TYPE_DSW",
        "SCHEDULE_CONFIG_TYPE_HPC",
        "SCHEDULE_CONFIG_TYPE_RAY_JOB",
    ),
}

_SCHEDULE_TYPE_USAGE = {
    "SCHEDULE_CONFIG_TYPE_DSW": "notebook",
    "SCHEDULE_CONFIG_TYPE_HPC": "hpc",
    "SCHEDULE_CONFIG_TYPE_RAY_JOB": "ray",
}


def _group_id(group: dict) -> str:
    return str(group.get("logic_compute_group_id") or group.get("id") or "").strip()


def _group_name(group: dict, fallback: str) -> str:
    return str(group.get("name") or group.get("logic_compute_group_name") or fallback).strip()


def _extract_gpu_type(price: dict) -> str:
    gpu_info = price.get("gpu_info") if isinstance(price.get("gpu_info"), dict) else {}
    return str(
        gpu_info.get("gpu_type_display")
        or gpu_info.get("gpu_type")
        or gpu_info.get("brand_name")
        or price.get("gpu_type")
        or ("CPU" if int(price.get("gpu_count") or 0) == 0 else "")
    ).strip()


def _extract_memory_gib(price: dict) -> int:
    value = (
        price.get("memory_size_gib") or price.get("memory_size") or price.get("memory_size_gb") or 0
    )
    try:
        return int(value)
    except Exception:
        return 0


def _usage_from_schedule_type(schedule_config_type: str) -> str:
    return _SCHEDULE_TYPE_USAGE.get(schedule_config_type, schedule_config_type.lower())


def _usage_sort_key(usage: str) -> int:
    order = {"auto": 0, "hpc": 1, "notebook": 2, "ray": 3}
    return order.get(usage, 99)


def _should_stop_after_match(usage: str) -> bool:
    return usage == "auto"


def _query_workspace_specs(
    *,
    session,  # noqa: ANN001
    workspace_id: str,
    workspace_name: str,
    usage: str,
    group_filter: str,
    include_empty: bool,
) -> list[dict]:
    """Query all (group × schedule_type) specs for one workspace."""
    schedule_types = _USAGE_SCHEDULE_TYPES[usage]
    rows: list[dict] = []
    seen_rows: set[tuple[str, str, str, int, int, int, str]] = set()

    groups = browser_api_module.list_notebook_compute_groups(
        workspace_id=workspace_id,
        session=session,
    )

    for item in groups:
        logic_compute_group_id = _group_id(item)
        if not logic_compute_group_id:
            continue
        compute_group_name = _group_name(item, fallback=logic_compute_group_id)
        if group_filter and group_filter not in compute_group_name.lower():
            continue

        matched_any = False
        for schedule_config_type in schedule_types:
            prices = browser_api_module.get_resource_prices(
                workspace_id=workspace_id,
                logic_compute_group_id=logic_compute_group_id,
                schedule_config_type=schedule_config_type,
                session=session,
            )
            usage_label = _usage_from_schedule_type(schedule_config_type)

            if not prices:
                if include_empty and usage != "auto":
                    empty_key = (
                        logic_compute_group_id,
                        usage_label,
                        "",
                        0,
                        0,
                        0,
                        "",
                    )
                    if empty_key not in seen_rows:
                        seen_rows.add(empty_key)
                        rows.append(
                            {
                                "workspace_id": workspace_id,
                                "workspace_name": workspace_name,
                                "usage": usage_label,
                                "schedule_config_type": schedule_config_type,
                                "compute_group_name": compute_group_name,
                                "logic_compute_group_id": logic_compute_group_id,
                                "spec_id": "",
                                "cpu_count": 0,
                                "memory_size_gib": 0,
                                "gpu_count": 0,
                                "gpu_type": "",
                                "total_price_per_hour": 0,
                            }
                        )
                continue

            for price in prices:
                spec_id = str(price.get("quota_id") or price.get("spec_id") or "").strip()
                cpu_count = int(price.get("cpu_count") or 0)
                memory_size_gib = _extract_memory_gib(price)
                gpu_count = int(price.get("gpu_count") or 0)
                gpu_type = _extract_gpu_type(price)
                row_key = (
                    logic_compute_group_id,
                    usage_label,
                    spec_id,
                    cpu_count,
                    memory_size_gib,
                    gpu_count,
                    gpu_type,
                )
                if row_key in seen_rows:
                    continue
                seen_rows.add(row_key)
                rows.append(
                    {
                        "workspace_id": workspace_id,
                        "workspace_name": workspace_name,
                        "usage": usage_label,
                        "schedule_config_type": schedule_config_type,
                        "compute_group_name": compute_group_name,
                        "logic_compute_group_id": logic_compute_group_id,
                        "spec_id": spec_id,
                        "cpu_count": cpu_count,
                        "memory_size_gib": memory_size_gib,
                        "gpu_count": gpu_count,
                        "gpu_type": gpu_type,
                        "total_price_per_hour": price.get("total_price_per_hour", 0),
                    }
                )
            matched_any = True
            if _should_stop_after_match(usage):
                break

        if matched_any:
            continue
        if include_empty and usage == "auto":
            empty_key = (
                logic_compute_group_id,
                "auto",
                "",
                0,
                0,
                0,
                "",
            )
            if empty_key not in seen_rows:
                seen_rows.add(empty_key)
                rows.append(
                    {
                        "workspace_id": workspace_id,
                        "workspace_name": workspace_name,
                        "usage": "auto",
                        "schedule_config_type": "",
                        "compute_group_name": compute_group_name,
                        "logic_compute_group_id": logic_compute_group_id,
                        "spec_id": "",
                        "cpu_count": 0,
                        "memory_size_gib": 0,
                        "gpu_count": 0,
                        "gpu_type": "",
                        "total_price_per_hour": 0,
                    }
                )

    return rows


def _resolve_query_workspaces(
    *,
    session,  # noqa: ANN001
    explicit_workspace: Optional[str],
    config: Config,
    usage: str,
    cross_search_requested: bool,
) -> list[tuple[str, str]]:
    """Pick which workspaces to query, returning a list of (id, display_name)."""
    if explicit_workspace:
        resolved = select_workspace_id(config, explicit_workspace_name=explicit_workspace)
        ws_id = resolved or session.workspace_id
        name = (getattr(session, "all_workspace_names", None) or {}).get(ws_id) or explicit_workspace
        return [(ws_id, name)]

    cross = cross_search_requested or usage == "ray"
    if cross:
        ws_ids = list(getattr(session, "all_workspace_ids", None) or [])
        names = getattr(session, "all_workspace_names", None) or {}
        if not ws_ids:
            ws_ids = [session.workspace_id]
        return [(wid, names.get(wid) or wid) for wid in ws_ids]

    ws_id = session.workspace_id
    name = (getattr(session, "all_workspace_names", None) or {}).get(ws_id) or ws_id
    return [(ws_id, name)]


@click.command("specs")
@click.option("--workspace", default=None, help="Workspace name (from [workspaces])")
@click.option(
    "--all-workspaces",
    "-A",
    "all_workspaces",
    is_flag=True,
    default=False,
    help=(
        "Search every workspace the account can see (auto-enabled for "
        "--usage ray since Ray quotas only exist in a few workspaces)."
    ),
)
@click.option("--group", default=None, help="Filter by compute group name (partial match)")
@click.option(
    "--usage",
    type=click.Choice(["auto", "notebook", "hpc", "ray", "all"], case_sensitive=False),
    default="auto",
    show_default=True,
    help=(
        "Spec family to query. auto = HPC first, fall back to notebook/DSW. "
        "Use 'ray' for Ray head/worker quotas (ray create --head-spec / --worker spec=)."
    ),
)
@click.option("--include-empty", is_flag=True, help="Include compute groups that return no specs")
@click.option("--json", "json_output_local", is_flag=True, help="Alias for global --json")
@pass_context
def list_specs(
    ctx: Context,
    workspace: Optional[str],
    all_workspaces: bool,
    group: Optional[str],
    usage: str,
    include_empty: bool,
    json_output_local: bool,
) -> None:
    """Discover resource specs for notebook / HPC / Ray creation.

    ``auto`` checks HPC quotas first and falls back to notebook/DSW quotas.
    Use ``--usage ray`` for Ray head/worker quotas (consumed by
    ``inspire ray create --head-spec`` / ``--worker spec=``); when
    ``--workspace`` is omitted, ``--usage ray`` searches every workspace
    automatically because Ray quotas live in only a handful of them.

    Returns per-spec entries including:
    - workspace_id / workspace_name
    - logic_compute_group_id
    - spec_id (quota_id)
    - cpu_count / memory_size_gib / gpu_count / gpu_type
    """

    ctx.json_output = bool(ctx.json_output or json_output_local)
    usage = usage.lower()
    try:
        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
        session = get_web_session()

        target_workspaces = _resolve_query_workspaces(
            session=session,
            explicit_workspace=workspace,
            config=config,
            usage=usage,
            cross_search_requested=all_workspaces,
        )

        group_filter = (group or "").strip().lower()
        rows: list[dict] = []
        for ws_id, ws_name in target_workspaces:
            rows.extend(
                _query_workspace_specs(
                    session=session,
                    workspace_id=ws_id,
                    workspace_name=ws_name,
                    usage=usage,
                    group_filter=group_filter,
                    include_empty=include_empty,
                )
            )

        rows.sort(
            key=lambda r: (
                str(r.get("workspace_name", "")),
                _usage_sort_key(str(r.get("usage", ""))),
                str(r.get("compute_group_name", "")),
                -int(r.get("gpu_count", 0)),
                -int(r.get("cpu_count", 0)),
                -int(r.get("memory_size_gib", 0)),
                str(r.get("spec_id", "")),
            )
        )

        if ctx.json_output:
            ws_ids = [w for w, _ in target_workspaces]
            ws_names = [n for _, n in target_workspaces]
            click.echo(
                json_formatter.format_json(
                    {
                        # First workspace kept for backwards compatibility with
                        # any tooling that read this field; the list form below
                        # is authoritative when multiple workspaces are searched.
                        "workspace_id": ws_ids[0] if ws_ids else "",
                        "workspace_ids": ws_ids,
                        "workspace_names": ws_names,
                        "usage_filter": usage,
                        "specs": rows,
                        "total": len(rows),
                    }
                )
            )
            return

        if not rows:
            click.echo("No resource specs found.")
            if usage == "ray" and len(target_workspaces) == 1:
                click.echo(
                    "Hint: Ray quotas are scoped to specific workspaces; "
                    "rerun with -A / --all-workspaces or --workspace <name>."
                )
            return

        # Show workspace column when more than one workspace was queried.
        multi_ws = len({r.get("workspace_id") for r in rows}) > 1
        if multi_ws:
            headers = (
                "Workspace",
                "Usage",
                "Compute Group",
                "Spec ID",
                "GPU",
                "CPU",
                "MemGiB",
                "Logic Group ID",
            )
            widths = [16, 8, 24, 36, 8, 6, 8, 36]
        else:
            headers = (
                "Usage",
                "Compute Group",
                "Spec ID",
                "GPU",
                "CPU",
                "MemGiB",
                "Logic Group ID",
            )
            widths = [10, 24, 36, 8, 6, 8, 36]

        click.echo("")
        click.echo("Resource Specs (for notebook create / hpc create / ray create)")
        click.echo("-" * (sum(widths) + len(widths) - 1))
        click.echo(" ".join(f"{h:<{w}}" for h, w in zip(headers, widths)))
        click.echo("-" * (sum(widths) + len(widths) - 1))
        for row in rows:
            gpu_desc = f"{row['gpu_count']}x{row['gpu_type'] or 'CPU'}"
            if multi_ws:
                cells = [
                    str(row.get("workspace_name", ""))[: widths[0] - 1],
                    str(row["usage"])[: widths[1] - 1],
                    str(row["compute_group_name"])[: widths[2] - 1],
                    str(row["spec_id"])[: widths[3] - 1],
                    gpu_desc[: widths[4] - 1],
                    str(row["cpu_count"]),
                    str(row["memory_size_gib"]),
                    str(row["logic_compute_group_id"])[: widths[7] - 1],
                ]
            else:
                cells = [
                    str(row["usage"])[: widths[0] - 1],
                    str(row["compute_group_name"])[: widths[1] - 1],
                    str(row["spec_id"])[: widths[2] - 1],
                    gpu_desc[: widths[3] - 1],
                    str(row["cpu_count"]),
                    str(row["memory_size_gib"]),
                    str(row["logic_compute_group_id"])[: widths[6] - 1],
                ]
            click.echo(" ".join(f"{c:<{w}}" for c, w in zip(cells, widths)))
        click.echo("-" * (sum(widths) + len(widths) - 1))
        if multi_ws:
            ws_summary = ", ".join(sorted({r.get("workspace_name", "") for r in rows}))
            click.echo(f"Workspaces searched: {ws_summary}")
        else:
            click.echo(f"Workspace: {target_workspaces[0][1]}")
        click.echo(f"Total specs: {len(rows)}")
        if usage == "hpc":
            click.echo(
                "Pass --compute-group <name>, --cpus-per-task <n>, --memory-per-cpu <n> to "
                "`inspire hpc create`; the CLI resolves spec_id live — no ID needed."
            )
        elif usage == "ray":
            click.echo(
                "Feed Spec ID to `inspire ray create --head-spec <id>` or "
                "`--worker 'spec=<id>,...'`."
            )
        elif usage == "all":
            click.echo(
                "Filter with --usage {notebook|hpc|ray} to focus on one family."
            )
        elif usage == "auto":
            click.echo(
                "Auto mode prefers HPC quotas and falls back to notebook quotas when HPC is unavailable."
            )
        elif usage == "notebook":
            click.echo(
                "Pick a triple (gpu_count, cpu_count, memory_size_gib) from the table and "
                "pass it as --quota gpu,cpu,mem to `inspire notebook create` (or `job create` / `run`)."
            )
        click.echo("")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = ["list_specs"]
