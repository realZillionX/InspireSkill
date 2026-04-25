"""Resources list command (availability)."""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.compute_groups import compute_group_name_map, load_compute_groups_from_config
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.resources import (
    KNOWN_COMPUTE_GROUPS,
    clear_availability_cache,
    fetch_resource_availability,
)
from inspire.platform.web.session import DEFAULT_WORKSPACE_ID, SessionExpiredError, get_web_session


def _known_compute_groups_from_config(*, show_all: bool) -> dict[str, str]:
    known_groups = KNOWN_COMPUTE_GROUPS
    if show_all:
        return known_groups

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        if config.compute_groups:
            groups_tuple = load_compute_groups_from_config(config.compute_groups)
            return compute_group_name_map(groups_tuple)
    except Exception:
        return known_groups
    return known_groups


def _workspace_name_map(
    *,
    config: Optional[Config],
    session,
) -> dict[str, str]:
    names = dict(session.all_workspace_names or {})
    if config is not None:
        for name, wid in getattr(config, "workspaces", {}).items():
            if wid and wid not in names:
                names[wid] = name
    return names


def _resolve_workspace_scope(
    *,
    config: Optional[Config],
    session,
    explicit_workspace_name: Optional[str],
    show_all: bool,
) -> tuple[list[str], dict[str, str], bool]:
    workspace_names = _workspace_name_map(config=config, session=session)
    if explicit_workspace_name is not None:
        if config is None:
            raise ConfigError("Workspace selection requires a loaded config.")
        resolved_workspace_id = select_workspace_id(
            config,
            explicit_workspace_name=explicit_workspace_name,
        )
        return [resolved_workspace_id], workspace_names, True

    if show_all:
        seen: set[str] = set()
        workspace_ids: list[str] = []
        for wid in session.all_workspace_ids or []:
            if wid and wid not in seen:
                seen.add(wid)
                workspace_ids.append(wid)
        if workspace_ids:
            return workspace_ids, workspace_names, False

    return [session.workspace_id or DEFAULT_WORKSPACE_ID], workspace_names, False


def _format_metric(value: float | int) -> str:
    numeric = float(value)
    if abs(numeric - round(numeric)) < 1e-6:
        return str(int(round(numeric)))
    return f"{numeric:.1f}"


def _format_availability_table(availability, workspace_mode: bool = False) -> None:
    title = "📊 GPU Availability (Workspace)" if workspace_mode else "📊 GPU Availability (Live)"
    scope_note = "Shows availability in your workspace only" if workspace_mode else ""

    lines = [
        "",
        title,
        "─" * 80,
    ]

    if scope_note:
        lines.append(f"{scope_note}")
        lines.append("─" * 80)

    lines.append(
        f"{'GPU Type':<12} {'Location':<25} {'Ready':<8} {'Free':<8} {'Free GPUs':<12}",
    )
    lines.append("─" * 80)

    for a in availability:
        location = a.group_name[:24]
        gpu_type = a.gpu_type[:11]

        free_gpus = a.free_gpus
        if free_gpus >= 8:
            status = ""
        elif free_gpus > 0:
            status = "⚠"
        else:
            status = "✗"

        lines.append(
            f"{gpu_type:<12} {location:<25} {a.ready_nodes:<8} {a.free_nodes:<8} "
            f"{free_gpus:<12} {status}"
        )

    lines.append("─" * 80)
    lines.append("")
    lines.append("💡 Usage:")
    lines.append('  inspire run "python train.py" -q 1,20,200   # 1 GPU + 20 CPU + 200 GiB')
    lines.append('  inspire run "python train.py" -q 4,80,800 --group H100   # Pin group')
    lines.append('  inspire resources specs --usage notebook    # List valid quota triples')
    lines.append("")

    click.echo("\n".join(lines))


def _format_accurate_availability_table(availability, *, include_cpu: bool) -> None:
    gpu_rows = [a for a in availability if getattr(a, "resource_kind", "gpu") == "gpu"]
    cpu_rows = [a for a in availability if getattr(a, "resource_kind", "gpu") == "cpu"]
    workspace_names = {
        str(getattr(a, "workspace_name", "") or getattr(a, "workspace_id", ""))
        for a in availability
    }
    show_workspace = len(workspace_names - {""}) > 1

    lines = ["", "📊 Compute Group Availability (Accurate Real-Time)"]

    if gpu_rows:
        widths = [14, 22, 25, 10, 8, 8, 8] if show_workspace else [22, 25, 10, 8, 8, 8]
        separator = "─" * (sum(widths) + len(widths) - 1)
        lines.append(separator)
        if show_workspace:
            lines.append(
                f"{'Workspace':<{widths[0]}} {'GPU Type':<{widths[1]}} {'Compute Group':<{widths[2]}} "
                f"{'Available':>{widths[3]}} {'Used':>{widths[4]}} {'Low Pri':>{widths[5]}} {'Total':>{widths[6]}}"
            )
        else:
            lines.append(
                f"{'GPU Type':<{widths[0]}} {'Compute Group':<{widths[1]}} "
                f"{'Available':>{widths[2]}} {'Used':>{widths[3]}} {'Low Pri':>{widths[4]}} {'Total':>{widths[5]}}"
            )
        lines.append(separator)

        sorted_gpu_rows = sorted(gpu_rows, key=lambda x: x.available_gpus, reverse=True)
        total_available = 0
        total_used = 0
        total_low_pri = 0
        total_gpus = 0

        for row in sorted_gpu_rows:
            available = row.available_gpus
            if available >= 100:
                status = "✓"
            elif available >= 32:
                status = "○"
            elif available >= 8:
                status = "◐"
            elif available > 0:
                status = "⚠"
            else:
                status = "✗"

            if show_workspace:
                lines.append(
                    f"{row.workspace_name[:widths[0]-1]:<{widths[0]}} "
                    f"{row.gpu_type[:widths[1]-1]:<{widths[1]}} "
                    f"{row.group_name[:widths[2]-1]:<{widths[2]}} "
                    f"{row.available_gpus:>{widths[3]}} {row.used_gpus:>{widths[4]}} "
                    f"{row.low_priority_gpus:>{widths[5]}} {row.total_gpus:>{widths[6]}} {status}"
                )
            else:
                lines.append(
                    f"{row.gpu_type[:widths[0]-1]:<{widths[0]}} "
                    f"{row.group_name[:widths[1]-1]:<{widths[1]}} "
                    f"{row.available_gpus:>{widths[2]}} {row.used_gpus:>{widths[3]}} "
                    f"{row.low_priority_gpus:>{widths[4]}} {row.total_gpus:>{widths[5]}} {status}"
                )

            total_available += row.available_gpus
            total_used += row.used_gpus
            total_low_pri += row.low_priority_gpus
            total_gpus += row.total_gpus

        lines.append(separator)
        if show_workspace:
            lines.append(
                f"{'TOTAL':<{widths[0]}} {'':<{widths[1]}} {'':<{widths[2]}} "
                f"{total_available:>{widths[3]}} {total_used:>{widths[4]}} "
                f"{total_low_pri:>{widths[5]}} {total_gpus:>{widths[6]}}"
            )
        else:
            lines.append(
                f"{'TOTAL':<{widths[0]}} {'':<{widths[1]}} {total_available:>{widths[2]}} "
                f"{total_used:>{widths[3]}} {total_low_pri:>{widths[4]}} {total_gpus:>{widths[5]}}"
            )

    if include_cpu and cpu_rows:
        widths = (
            [14, 25, 10, 10, 10, 12, 12, 12] if show_workspace else [25, 10, 10, 10, 12, 12, 12]
        )
        separator = "─" * (sum(widths) + len(widths) - 1)
        lines.append("")
        lines.append("CPU-Only Compute Groups")
        lines.append(separator)
        if show_workspace:
            lines.append(
                f"{'Workspace':<{widths[0]}} {'Compute Group':<{widths[1]}} "
                f"{'Avail CPU':>{widths[2]}} {'Used CPU':>{widths[3]}} {'Total CPU':>{widths[4]}} "
                f"{'Avail GiB':>{widths[5]}} {'Used GiB':>{widths[6]}} {'Total GiB':>{widths[7]}}"
            )
        else:
            lines.append(
                f"{'Compute Group':<{widths[0]}} {'Avail CPU':>{widths[1]}} {'Used CPU':>{widths[2]}} "
                f"{'Total CPU':>{widths[3]}} {'Avail GiB':>{widths[4]}} {'Used GiB':>{widths[5]}} {'Total GiB':>{widths[6]}}"
            )
        lines.append(separator)

        sorted_cpu_rows = sorted(cpu_rows, key=lambda x: x.cpu_available, reverse=True)
        total_cpu_available = 0.0
        total_cpu_used = 0.0
        total_cpu = 0.0
        total_mem_available = 0.0
        total_mem_used = 0.0
        total_mem = 0.0

        for row in sorted_cpu_rows:
            if show_workspace:
                lines.append(
                    f"{row.workspace_name[:widths[0]-1]:<{widths[0]}} "
                    f"{row.group_name[:widths[1]-1]:<{widths[1]}} "
                    f"{_format_metric(row.cpu_available):>{widths[2]}} "
                    f"{_format_metric(row.cpu_used):>{widths[3]}} "
                    f"{_format_metric(row.cpu_total):>{widths[4]}} "
                    f"{_format_metric(row.memory_available_gib):>{widths[5]}} "
                    f"{_format_metric(row.memory_used_gib):>{widths[6]}} "
                    f"{_format_metric(row.memory_total_gib):>{widths[7]}}"
                )
            else:
                lines.append(
                    f"{row.group_name[:widths[0]-1]:<{widths[0]}} "
                    f"{_format_metric(row.cpu_available):>{widths[1]}} "
                    f"{_format_metric(row.cpu_used):>{widths[2]}} "
                    f"{_format_metric(row.cpu_total):>{widths[3]}} "
                    f"{_format_metric(row.memory_available_gib):>{widths[4]}} "
                    f"{_format_metric(row.memory_used_gib):>{widths[5]}} "
                    f"{_format_metric(row.memory_total_gib):>{widths[6]}}"
                )

            total_cpu_available += row.cpu_available
            total_cpu_used += row.cpu_used
            total_cpu += row.cpu_total
            total_mem_available += row.memory_available_gib
            total_mem_used += row.memory_used_gib
            total_mem += row.memory_total_gib

        lines.append(separator)
        if show_workspace:
            lines.append(
                f"{'TOTAL':<{widths[0]}} {'':<{widths[1]}} "
                f"{_format_metric(total_cpu_available):>{widths[2]}} "
                f"{_format_metric(total_cpu_used):>{widths[3]}} "
                f"{_format_metric(total_cpu):>{widths[4]}} "
                f"{_format_metric(total_mem_available):>{widths[5]}} "
                f"{_format_metric(total_mem_used):>{widths[6]}} "
                f"{_format_metric(total_mem):>{widths[7]}}"
            )
        else:
            lines.append(
                f"{'TOTAL':<{widths[0]}} {_format_metric(total_cpu_available):>{widths[1]}} "
                f"{_format_metric(total_cpu_used):>{widths[2]}} {_format_metric(total_cpu):>{widths[3]}} "
                f"{_format_metric(total_mem_available):>{widths[4]}} {_format_metric(total_mem_used):>{widths[5]}} "
                f"{_format_metric(total_mem):>{widths[6]}}"
            )

    lines.append("")
    lines.append("💡 Legend:")
    lines.append(
        "  Available = platform-reported total minus used; negative values come from the platform API"
    )
    if include_cpu:
        lines.append("  CPU rows   = CPU-only compute groups with CPU and memory totals")
    lines.append("")
    lines.append("💡 Usage:")
    lines.append('  inspire run "python train.py" -q 1,20,200   # 1 GPU + 20 CPU + 200 GiB')
    lines.append('  inspire run "python train.py" -q 4,80,800 --group H100   # Pin group')
    lines.append('  inspire resources specs --usage notebook    # List valid quota triples')
    lines.append("")

    click.echo("\n".join(lines))


def _list_accurate_resources(
    ctx: Context,
    show_all: bool,
    *,
    explicit_workspace_name: Optional[str],
    include_cpu: bool,
) -> None:
    """List accurate compute-group availability using browser API."""
    try:
        config = None
        try:
            config, _ = Config.from_files_and_env(
                require_credentials=False, require_target_dir=False
            )
        except Exception:
            config = None

        session = get_web_session()
        workspace_ids, workspace_names, explicit_workspace_selected = _resolve_workspace_scope(
            config=config,
            session=session,
            explicit_workspace_name=explicit_workspace_name,
            show_all=show_all,
        )
        target_workspace_id = workspace_ids[0] if len(workspace_ids) == 1 else None

        known_groups = _known_compute_groups_from_config(
            show_all=show_all or explicit_workspace_selected
        )

        availability = browser_api_module.get_accurate_resource_availability(
            workspace_id=target_workspace_id,
            session=session,
            include_cpu=include_cpu,
            all_workspaces=show_all and not explicit_workspace_selected,
        )

        if not show_all and not explicit_workspace_selected:
            availability = [a for a in availability if a.group_id in known_groups]
            for entry in availability:
                if not entry.group_name:
                    entry.group_name = known_groups.get(entry.group_id, entry.group_name)
        for entry in availability:
            if not entry.workspace_name:
                entry.workspace_name = workspace_names.get(entry.workspace_id, entry.workspace_name)

        if not availability:
            if ctx.json_output:
                click.echo(json_formatter.format_json({"availability": []}))
            else:
                click.echo(human_formatter.format_error("No compute resources found"))
            return

        if ctx.json_output:
            output = [
                {
                    "workspace_id": a.workspace_id,
                    "workspace_name": a.workspace_name,
                    "group_id": a.group_id,
                    "group_name": a.group_name,
                    "resource_kind": a.resource_kind,
                    "gpu_type": a.gpu_type,
                    "total_gpus": a.total_gpus,
                    "used_gpus": a.used_gpus,
                    "available_gpus": a.available_gpus,
                    "low_priority_gpus": a.low_priority_gpus,
                    "cpu_total": a.cpu_total,
                    "cpu_used": a.cpu_used,
                    "cpu_available": a.cpu_available,
                    "memory_total_gib": a.memory_total_gib,
                    "memory_used_gib": a.memory_used_gib,
                    "memory_available_gib": a.memory_available_gib,
                }
                for a in availability
            ]
            click.echo(json_formatter.format_json({"availability": output}))
        else:
            _format_accurate_availability_table(availability, include_cpu=include_cpu)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def _list_workspace_resources(ctx: Context, show_all: bool, no_cache: bool) -> None:
    """List workspace-specific GPU availability using browser API."""
    try:
        if no_cache:
            clear_availability_cache()

        config = None
        try:
            config, _ = Config.from_files_and_env(require_credentials=False)
        except Exception:
            pass

        availability = fetch_resource_availability(
            config=config,
            known_only=not show_all,
        )

        if not availability:
            click.echo(human_formatter.format_error("No GPU resources found in your workspace"))
            return

        if ctx.json_output:
            output = [
                {
                    "group_id": a.group_id,
                    "group_name": a.group_name,
                    "gpu_type": a.gpu_type,
                    "gpus_per_node": a.gpu_per_node,
                    "total_nodes": a.total_nodes,
                    "ready_nodes": a.ready_nodes,
                    "free_nodes": a.free_nodes,
                    "free_gpus": a.free_gpus,
                }
                for a in availability
            ]
            click.echo(json_formatter.format_json({"availability": output}))
            return

        _format_availability_table(availability, workspace_mode=True)

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def _progress_bar(current: int, total: int, width: int = 20) -> str:
    if total == 0:
        return "░" * width
    filled = int(width * current / total)
    return "█" * filled + "░" * (width - filled)


def _render_nodes_display(
    availability: list,
    *,
    phase: str,
    timestamp: str,
    interval: int,
    progress_state: dict[str, int],
) -> None:
    os.system("clear")

    if phase == "fetching":
        fetched = progress_state["fetched"]
        total = progress_state["total"] or 1
        bar = _progress_bar(fetched, total)
        if total > 1:
            click.echo(f"🔄 [{bar}] Fetching {fetched}/{total} nodes...\n")
        else:
            click.echo(f"🔄 [{bar}] Fetching availability...\n")
    else:
        bar = _progress_bar(1, 1)
        click.echo(f"✅ [{bar}] Updated at {timestamp} (Workspace) (interval: {interval}s)\n")

    if not availability:
        if phase != "fetching":
            click.echo("No GPU resources found")
        return

    click.echo("─" * 60)
    click.echo(f"{'GPU':<6} {'Location':<24} {'Ready':>8} {'Free':>8} {'GPUs':>8}")
    click.echo("─" * 60)

    total_free = 0
    for a in availability:
        location = a.group_name[:23]
        gpu = a.gpu_type[:5]
        free_gpus = a.free_gpus
        total_free += free_gpus

        if free_gpus >= 64:
            indicator = "🟢"
        elif free_gpus >= 16:
            indicator = "🟡"
        elif free_gpus > 0:
            indicator = "🟠"
        else:
            indicator = "🔴"

        click.echo(
            f"{gpu:<6} {location:<24} {a.ready_nodes:>8} {a.free_nodes:>8} "
            f"{free_gpus:>8} {indicator}"
        )

    click.echo("─" * 60)
    click.echo(f"{'Total':<6} {'':<24} {'':>8} {'':>8} {total_free:>8}")
    click.echo("")
    click.echo("Ctrl+C to stop")


def _render_accurate_display(
    availability: list,
    *,
    phase: str,
    timestamp: str,
    interval: int,
) -> None:
    os.system("clear")

    if phase == "fetching":
        click.echo("🔄 Fetching accurate availability...\n")
    else:
        click.echo(f"✅ Updated at {timestamp} (Accurate) (interval: {interval}s)\n")

    if not availability:
        if phase != "fetching":
            click.echo("No GPU resources found")
        return

    lines = [
        "─" * 95,
        (
            f"{'GPU Type':<22} {'Compute Group':<25} {'Available':>10} "
            f"{'Used':>8} {'Low Pri':>8} {'Total':>8}"
        ),
        "─" * 95,
    ]

    sorted_avail = sorted(availability, key=lambda x: x.available_gpus, reverse=True)

    total_available = 0
    total_used = 0
    total_low_pri = 0
    total_gpus = 0

    for a in sorted_avail:
        gpu_type = a.gpu_type[:21]
        location = a.group_name[:24]
        free_gpus = a.available_gpus

        if free_gpus >= 100:
            status = "✓"
        elif free_gpus >= 32:
            status = "○"
        elif free_gpus >= 8:
            status = "◐"
        elif free_gpus > 0:
            status = "⚠"
        else:
            status = "✗"

        lines.append(
            f"{gpu_type:<22} {location:<25} {a.available_gpus:>10} {a.used_gpus:>8} "
            f"{a.low_priority_gpus:>8} {a.total_gpus:>8} {status}"
        )

        total_available += a.available_gpus
        total_used += a.used_gpus
        total_low_pri += a.low_priority_gpus
        total_gpus += a.total_gpus

    lines.append("─" * 95)
    lines.append(
        f"{'TOTAL':<22} {'':<25} {total_available:>10} {total_used:>8} "
        f"{total_low_pri:>8} {total_gpus:>8}"
    )
    lines.append("")
    lines.append("Ctrl+C to stop")

    click.echo("\n".join(lines))


def _render_display(
    *,
    mode: str,
    availability: list,
    phase: str,
    timestamp: str,
    interval: int,
    progress_state: dict[str, int],
) -> None:
    if mode == "nodes":
        _render_nodes_display(
            availability,
            phase=phase,
            timestamp=timestamp,
            interval=interval,
            progress_state=progress_state,
        )
    else:
        _render_accurate_display(availability, phase=phase, timestamp=timestamp, interval=interval)


def _watch_resources(
    ctx: Context,
    show_all: bool,
    interval: int,
    workspace: bool,
    use_global: bool,
) -> None:
    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level
    api_logger.setLevel(logging.CRITICAL)

    mode = "nodes" if workspace or use_global else "accurate"

    try:
        if mode == "nodes":
            get_web_session(require_workspace=True)
        else:
            get_web_session()
    except Exception as e:
        click.echo(human_formatter.format_error(f"Failed to get web session: {e}"), err=True)
        sys.exit(EXIT_AUTH_ERROR)

    progress_state = {"fetched": 0, "total": 0}

    def on_progress(fetched: int, total: int) -> None:
        if mode != "nodes":
            return
        progress_state["fetched"] = fetched
        progress_state["total"] = total
        now = datetime.now().strftime("%H:%M:%S")
        _render_display(
            mode=mode,
            availability=availability,
            phase="fetching",
            timestamp=now,
            interval=interval,
            progress_state=progress_state,
        )

    try:
        availability: list = []
        while True:
            progress_state["fetched"] = 0
            progress_state["total"] = 0

            now = datetime.now().strftime("%H:%M:%S")
            _render_display(
                mode=mode,
                availability=availability,
                phase="fetching",
                timestamp=now,
                interval=interval,
                progress_state=progress_state,
            )

            try:
                if mode == "nodes":
                    clear_availability_cache()
                    config = None
                    try:
                        config, _ = Config.from_files_and_env(require_credentials=False)
                    except Exception:
                        pass
                    availability = fetch_resource_availability(
                        config=config,
                        known_only=not show_all,
                        progress_callback=on_progress,
                    )
                else:
                    availability = browser_api_module.get_accurate_gpu_availability()
                    known_groups = _known_compute_groups_from_config(show_all=show_all)
                    if not show_all:
                        availability = [a for a in availability if a.group_id in known_groups]
                        for entry in availability:
                            if not entry.group_name:
                                entry.group_name = known_groups.get(
                                    entry.group_id, entry.group_name
                                )
            except (SessionExpiredError, ValueError) as e:
                api_logger.setLevel(original_level)
                click.echo(human_formatter.format_error(str(e)), err=True)
                sys.exit(EXIT_AUTH_ERROR)
            except Exception as e:
                os.system("clear")
                click.echo(f"⚠️  API error: {e}")
                click.echo(f"Retrying in {interval}s...")
                time.sleep(interval)
                continue

            now = datetime.now().strftime("%H:%M:%S")
            _render_display(
                mode=mode,
                availability=availability,
                phase="done",
                timestamp=now,
                interval=interval,
                progress_state=progress_state,
            )

            time.sleep(interval)

    except KeyboardInterrupt:
        click.echo("\nStopped watching.")
        sys.exit(0)
    finally:
        api_logger.setLevel(original_level)


def run_resources_list(
    ctx: Context,
    *,
    no_cache: bool,
    show_all: bool,
    watch: bool,
    interval: int,
    workspace: bool,
    use_global: bool,
    explicit_workspace_name: Optional[str],
    include_cpu: bool,
) -> None:
    if include_cpu and (workspace or use_global):
        _handle_error(
            ctx,
            "InvalidOption",
            "CPU totals are only available in accurate mode. Remove --workspace/--global.",
            EXIT_CONFIG_ERROR,
        )
        return

    if watch:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "InvalidOption",
                    "Watch mode not supported with JSON output",
                    EXIT_CONFIG_ERROR,
                ),
                err=True,
            )
            sys.exit(EXIT_CONFIG_ERROR)

        _watch_resources(ctx, show_all, interval, workspace, use_global)
        return

    if workspace or use_global:
        if use_global and not workspace:
            click.echo(
                "Note: --global is deprecated; showing workspace node availability instead.",
                err=True,
            )
        _list_workspace_resources(ctx, show_all, no_cache)
        return

    _list_accurate_resources(
        ctx,
        show_all,
        explicit_workspace_name=explicit_workspace_name,
        include_cpu=include_cpu,
    )


@click.command("list")
@click.option(
    "--no-cache",
    is_flag=True,
    help="Bypass cached node availability (workspace view only)",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Thorough check: show all accessible compute groups across visible workspaces",
)
@click.option(
    "--workspace-name",
    default=None,
    help="Workspace name override for accurate mode",
)
@click.option(
    "--include-cpu",
    is_flag=True,
    help="Include CPU-only compute groups with CPU and memory totals (accurate mode only)",
)
@click.option(
    "--watch",
    "-w",
    is_flag=True,
    help="Continuously watch availability (refreshes every 30s)",
)
@click.option(
    "--interval",
    "-i",
    type=int,
    default=30,
    help="Watch refresh interval in seconds (default: 30)",
)
@click.option(
    "--workspace",
    "-ws",
    is_flag=True,
    help="Show per-node availability (workspace-scoped, browser API)",
)
@click.option(
    "--global",
    "use_global",
    is_flag=True,
    help="Deprecated: alias for --workspace (OpenAPI view removed)",
)
@pass_context
def list_resources(
    ctx: Context,
    no_cache: bool,
    show_all: bool,
    workspace_name: Optional[str],
    include_cpu: bool,
    watch: bool,
    interval: int,
    workspace: bool = False,
    use_global: bool = False,
) -> None:
    """List compute-group availability across workspaces.

    By default, shows accurate real-time GPU usage via browser API.
    Use --include-cpu to include CPU-only compute groups and CPU/memory totals.
    Use --workspace for per-node availability (free/ready nodes).

    \b
    Examples:
        inspire resources list              # Accurate GPU usage (default)
        inspire resources list --include-cpu  # Include CPU-only groups
        inspire resources list --workspace-name 分布式训练空间
        inspire resources list --workspace  # Node-level availability
        inspire resources list --all        # Include all visible workspaces/groups
        inspire resources list --watch      # Watch mode
    """
    run_resources_list(
        ctx,
        no_cache=no_cache,
        show_all=show_all,
        watch=watch,
        interval=interval,
        workspace=workspace,
        use_global=use_global,
        explicit_workspace_name=workspace_name,
        include_cpu=include_cpu,
    )
