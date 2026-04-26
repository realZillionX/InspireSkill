"""Resources specs command (discover quotas by name, no UUIDs surfaced)."""

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
    "notebook": ("SCHEDULE_CONFIG_TYPE_DSW",),
    "job": ("SCHEDULE_CONFIG_TYPE_TRAIN",),
    "hpc": ("SCHEDULE_CONFIG_TYPE_HPC",),
    "ray": ("SCHEDULE_CONFIG_TYPE_RAY_JOB",),
    "all": (
        "SCHEDULE_CONFIG_TYPE_DSW",
        "SCHEDULE_CONFIG_TYPE_TRAIN",
        "SCHEDULE_CONFIG_TYPE_HPC",
        "SCHEDULE_CONFIG_TYPE_RAY_JOB",
    ),
}

_SCHEDULE_TYPE_USAGE = {
    "SCHEDULE_CONFIG_TYPE_DSW": "notebook",
    "SCHEDULE_CONFIG_TYPE_TRAIN": "job",
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
    order = {"hpc": 0, "notebook": 1, "job": 2, "ray": 3}
    return order.get(usage, 99)


def _query_workspace_specs(
    *,
    session,  # noqa: ANN001
    workspace_id: str,
    workspace_name: str,
    usage: str,
    group_filter: str,
    include_empty: bool,
) -> list[dict]:
    """Query all (group × schedule_type) specs for one workspace.

    Each emitted row carries names only (workspace, compute group, gpu
    type, usage family) and the (gpu_count, cpu_count, memory_size_gib)
    triple. Internal UUIDs (workspace_id, logic_compute_group_id, raw
    quota IDs) stay inside this function as request inputs and never
    surface — callers describe quotas by the triple plus group name and
    let the CLI resolve them.
    """
    schedule_types = _USAGE_SCHEDULE_TYPES[usage]
    rows: list[dict] = []
    seen_rows: set[tuple[str, str, int, int, int, str]] = set()

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

        for schedule_config_type in schedule_types:
            prices = browser_api_module.get_resource_prices(
                workspace_id=workspace_id,
                logic_compute_group_id=logic_compute_group_id,
                schedule_config_type=schedule_config_type,
                session=session,
            )
            usage_label = _usage_from_schedule_type(schedule_config_type)

            if not prices:
                if include_empty:
                    empty_key = (compute_group_name, usage_label, 0, 0, 0, "")
                    if empty_key not in seen_rows:
                        seen_rows.add(empty_key)
                        rows.append(
                            {
                                "workspace_name": workspace_name,
                                "usage": usage_label,
                                "compute_group_name": compute_group_name,
                                "cpu_count": 0,
                                "memory_size_gib": 0,
                                "gpu_count": 0,
                                "gpu_type": "",
                            }
                        )
                continue

            for price in prices:
                cpu_count = int(price.get("cpu_count") or 0)
                memory_size_gib = _extract_memory_gib(price)
                gpu_count = int(price.get("gpu_count") or 0)
                gpu_type = _extract_gpu_type(price)
                row_key = (
                    compute_group_name,
                    usage_label,
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
                        "workspace_name": workspace_name,
                        "usage": usage_label,
                        "compute_group_name": compute_group_name,
                        "cpu_count": cpu_count,
                        "memory_size_gib": memory_size_gib,
                        "gpu_count": gpu_count,
                        "gpu_type": gpu_type,
                    }
                )

    return rows


def _resolve_query_workspaces(
    *,
    session,  # noqa: ANN001
    explicit_workspace: Optional[str],
    config: Config,
) -> list[str]:
    """Pick which workspaces to query, returning workspace display names.

    Default sweeps every workspace the account can see. UUIDs are looked up
    from the names internally — they never surface to the caller.
    """
    if explicit_workspace:
        resolved = select_workspace_id(config, explicit_workspace_name=explicit_workspace)
        if not resolved:
            return [explicit_workspace]
        names = getattr(session, "all_workspace_names", None) or {}
        return [names.get(resolved) or explicit_workspace]

    ws_ids = list(getattr(session, "all_workspace_ids", None) or [])
    names = getattr(session, "all_workspace_names", None) or {}
    if not ws_ids:
        return [names.get(session.workspace_id) or session.workspace_id]
    return [names.get(wid) or wid for wid in ws_ids]


def _name_to_id(session, config: Config, ws_name: str) -> str:  # noqa: ANN001
    """Map a workspace name back to its UUID for API calls."""
    try:
        resolved = select_workspace_id(config, explicit_workspace_name=ws_name)
        if resolved:
            return resolved
    except ConfigError:
        pass
    names = getattr(session, "all_workspace_names", None) or {}
    for wid, name in names.items():
        if name == ws_name:
            return wid
    return session.workspace_id


@click.command("specs")
@click.option(
    "--workspace",
    default=None,
    help=(
        "Workspace name (from [workspaces]). Omit to sweep every workspace "
        "the account can see."
    ),
)
@click.option("--group", default=None, help="Filter by compute group name (partial match)")
@click.option(
    "--usage",
    type=click.Choice(["all", "notebook", "job", "hpc", "ray"], case_sensitive=False),
    default="all",
    show_default=True,
    help=(
        "Spec family to query. 'all' returns notebook + job (TRAIN) + "
        "hpc + ray; narrow with the others."
    ),
)
@click.option("--include-empty", is_flag=True, help="Include compute groups that return no specs")
@click.option("--json", "json_output_local", is_flag=True, help="Alias for global --json")
@pass_context
def list_specs(
    ctx: Context,
    workspace: Optional[str],
    group: Optional[str],
    usage: str,
    include_empty: bool,
    json_output_local: bool,
) -> None:
    """Discover resource specs for notebook / HPC / Ray creation.

    Default sweeps every workspace the account can see; pass
    ``--workspace <name>`` to pin to one. ``--usage`` defaults to ``all``
    so notebook + job + hpc + ray quotas surface together; narrow when
    you only care about one family.

    Each row carries human-readable names (workspace, compute group,
    GPU type) plus the (gpu, cpu, memory) triple. Feed the triple back
    via ``--quota gpu,cpu,mem`` to ``inspire notebook create`` /
    ``job create`` / ``run`` / ``ray create --head-quota`` /
    ``--worker quota=...``; the CLI resolves it to the underlying
    platform handle.
    """

    ctx.json_output = bool(ctx.json_output or json_output_local)
    usage = usage.lower()
    try:
        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
        session = get_web_session()

        target_workspace_names = _resolve_query_workspaces(
            session=session,
            explicit_workspace=workspace,
            config=config,
        )

        group_filter = (group or "").strip().lower()
        rows: list[dict] = []
        for ws_name in target_workspace_names:
            ws_id = _name_to_id(session, config, ws_name)
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
            )
        )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "workspace_names": target_workspace_names,
                        "usage_filter": usage,
                        "specs": rows,
                        "total": len(rows),
                    }
                )
            )
            return

        if not rows:
            click.echo("No resource specs found.")
            return

        # Show workspace column when more than one workspace was queried.
        multi_ws = len({r.get("workspace_name") for r in rows}) > 1
        if multi_ws:
            headers = ("Workspace", "Usage", "Compute Group", "GPU", "CPU", "MemGiB")
            widths = [18, 9, 26, 10, 6, 8]
        else:
            headers = ("Usage", "Compute Group", "GPU", "CPU", "MemGiB")
            widths = [9, 26, 10, 6, 8]

        click.echo("")
        click.echo("Resource Specs (for notebook / hpc / ray / job / run create)")
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
                    gpu_desc[: widths[3] - 1],
                    str(row["cpu_count"]),
                    str(row["memory_size_gib"]),
                ]
            else:
                cells = [
                    str(row["usage"])[: widths[0] - 1],
                    str(row["compute_group_name"])[: widths[1] - 1],
                    gpu_desc[: widths[2] - 1],
                    str(row["cpu_count"]),
                    str(row["memory_size_gib"]),
                ]
            click.echo(" ".join(f"{c:<{w}}" for c, w in zip(cells, widths)))
        click.echo("-" * (sum(widths) + len(widths) - 1))
        if multi_ws:
            ws_summary = ", ".join(sorted({r.get("workspace_name", "") for r in rows}))
            click.echo(f"Workspaces searched: {ws_summary}")
        else:
            click.echo(f"Workspace: {target_workspace_names[0]}")
        click.echo(f"Total specs: {len(rows)}")
        if usage == "hpc":
            click.echo(
                "Pass --compute-group <name>, --cpus-per-task <n>, --memory-per-cpu <n> "
                "to `inspire hpc create`."
            )
        elif usage == "ray":
            click.echo(
                "Pick a row and pass its (gpu, cpu, memory_size_gib) triple to "
                "`inspire ray create --head-quota gpu,cpu,mem` and "
                "`--worker 'quota=gpu,cpu,mem;...'`."
            )
        elif usage == "notebook":
            click.echo(
                "Pick a row and pass its (gpu, cpu, memory_size_gib) triple as "
                "--quota gpu,cpu,mem to `inspire notebook create` (or `job create` / `run`)."
            )
        click.echo("")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = ["list_specs"]
