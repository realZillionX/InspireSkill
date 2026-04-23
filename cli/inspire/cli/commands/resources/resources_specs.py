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
    "all": ("SCHEDULE_CONFIG_TYPE_DSW", "SCHEDULE_CONFIG_TYPE_HPC"),
}

_SCHEDULE_TYPE_USAGE = {
    "SCHEDULE_CONFIG_TYPE_DSW": "notebook",
    "SCHEDULE_CONFIG_TYPE_HPC": "hpc",
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
    order = {"auto": 0, "hpc": 1, "notebook": 2}
    return order.get(usage, 99)


def _should_stop_after_match(usage: str) -> bool:
    return usage == "auto"


@click.command("specs")
@click.option("--workspace", default=None, help="Workspace name (from [workspaces])")
@click.option("--group", default=None, help="Filter by compute group name (partial match)")
@click.option(
    "--usage",
    type=click.Choice(["auto", "notebook", "hpc", "all"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="Spec family to query (auto = HPC first, then notebook/DSW)",
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
    """Discover resource specs for notebook/HPC creation.

    ``auto`` checks HPC quotas first and falls back to notebook/DSW quotas.

    Returns per-spec entries including:
    - logic_compute_group_id
    - spec_id (quota_id)
    - cpu_count / memory_size_gib / gpu_count / gpu_type
    - workspace_id
    """

    ctx.json_output = bool(ctx.json_output or json_output_local)
    usage = usage.lower()
    try:
        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
        resolved_workspace_id = select_workspace_id(
            config,
            explicit_workspace_name=workspace,
        )
        session = get_web_session()
        workspace_id = resolved_workspace_id or session.workspace_id

        groups = browser_api_module.list_notebook_compute_groups(
            workspace_id=workspace_id,
            session=session,
        )

        group_filter = (group or "").strip().lower()
        rows: list[dict] = []
        seen_rows: set[tuple[str, str, str, int, int, int, str]] = set()
        schedule_types = _USAGE_SCHEDULE_TYPES[usage]
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

        rows.sort(
            key=lambda r: (
                _usage_sort_key(str(r.get("usage", ""))),
                str(r.get("compute_group_name", "")),
                -int(r.get("gpu_count", 0)),
                -int(r.get("cpu_count", 0)),
                -int(r.get("memory_size_gib", 0)),
                str(r.get("spec_id", "")),
            )
        )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "workspace_id": workspace_id,
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
        click.echo("Resource Specs (for notebook create / hpc create)")
        click.echo("-" * (sum(widths) + len(widths) - 1))
        click.echo(
            f"{headers[0]:<{widths[0]}} {headers[1]:<{widths[1]}} "
            f"{headers[2]:<{widths[2]}} {headers[3]:<{widths[3]}} "
            f"{headers[4]:<{widths[4]}} {headers[5]:<{widths[5]}} "
            f"{headers[6]:<{widths[6]}}"
        )
        click.echo("-" * (sum(widths) + len(widths) - 1))
        for row in rows:
            gpu_desc = f"{row['gpu_count']}x{row['gpu_type'] or 'CPU'}"
            click.echo(
                f"{row['usage'][:widths[0]-1]:<{widths[0]}} "
                f"{row['compute_group_name'][:widths[1]-1]:<{widths[1]}} "
                f"{row['spec_id'][:widths[2]-1]:<{widths[2]}} "
                f"{gpu_desc[:widths[3]-1]:<{widths[3]}} "
                f"{row['cpu_count']:<{widths[4]}} "
                f"{row['memory_size_gib']:<{widths[5]}} "
                f"{row['logic_compute_group_id'][:widths[6]-1]:<{widths[6]}}"
            )
        click.echo("-" * (sum(widths) + len(widths) - 1))
        click.echo(f"Workspace: {workspace_id}")
        click.echo(f"Total specs: {len(rows)}")
        if usage == "hpc":
            click.echo(
                "Pass --compute-group <name>, --cpus-per-task <n>, --memory-per-cpu <n> to "
                "`inspire hpc create`; the CLI resolves spec_id live — no ID needed."
            )
        elif usage == "all":
            click.echo(
                "Use --usage notebook for notebook quotas and --usage hpc for HPC quotas."
            )
        elif usage == "auto":
            click.echo(
                "Auto mode prefers HPC quotas and falls back to notebook quotas when HPC is unavailable."
            )
        else:
            click.echo("Use --usage hpc to discover HPC quotas for `inspire hpc create`.")
        click.echo("")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = ["list_specs"]
