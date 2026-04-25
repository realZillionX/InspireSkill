"""Notebook GPU telemetry command (`inspire notebook top`)."""

from __future__ import annotations

import concurrent.futures
import csv
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Optional

import click

from inspire.bridge.tunnel import is_tunnel_available, load_tunnel_config, run_ssh_command
from inspire.bridge.tunnel.models import BridgeProfile, TunnelConfig
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import resolve_json_output

_NVIDIA_SMI_QUERY = (
    "nvidia-smi "
    "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu "
    "--format=csv,noheader,nounits"
)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bridge_notebook_id(bridge: BridgeProfile) -> str:
    return str(getattr(bridge, "notebook_id", "") or "").strip()


def _select_target_bridges(
    ctx: Context,
    *,
    tunnel_config: TunnelConfig,
    bridge_name: Optional[str],
) -> list[BridgeProfile]:
    if bridge_name:
        bridge = tunnel_config.get_bridge(bridge_name)
        if bridge is None:
            _handle_error(
                ctx,
                "ConfigError",
                f"Bridge '{bridge_name}' not found.",
                EXIT_CONFIG_ERROR,
                hint="Run 'inspire notebook connections' to see cached notebook names.",
            )
        notebook_id = _bridge_notebook_id(bridge)
        if not notebook_id:
            _handle_error(
                ctx,
                "ConfigError",
                (
                    f"Bridge '{bridge_name}' is not notebook-backed "
                    "(missing notebook_id metadata)."
                ),
                EXIT_CONFIG_ERROR,
                hint="Recreate it with 'inspire notebook ssh <notebook>'.",
            )
        return [bridge]

    bridges = [b for b in tunnel_config.list_bridges() if _bridge_notebook_id(b)]
    if not bridges:
        _handle_error(
            ctx,
            "ConfigError",
            "No notebook-backed tunnel profiles found.",
            EXIT_CONFIG_ERROR,
            hint=("Create one first: " "'inspire notebook ssh <notebook>'."),
        )

    return sorted(bridges, key=lambda b: b.name)


def _parse_float(value: str) -> float:
    token = value.strip()
    if not token or token.upper() == "N/A":
        return 0.0
    return float(token)


def _parse_int(value: str) -> int:
    token = value.strip()
    if not token or token.upper() == "N/A":
        return 0
    return int(float(token))


def _parse_nvidia_smi_output(stdout: str, stderr: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        stderr_msg = stderr.strip()
        if stderr_msg:
            raise ValueError(stderr_msg.splitlines()[0])
        raise ValueError("No GPU rows returned from nvidia-smi.")

    gpus: list[dict[str, Any]] = []
    reader = csv.reader(lines)
    for row in reader:
        if len(row) < 5:
            raise ValueError("Unexpected nvidia-smi output format.")
        try:
            gpus.append(
                {
                    "index": _parse_int(row[0]),
                    "util_percent": _parse_float(row[1]),
                    "mem_used_mib": _parse_int(row[2]),
                    "mem_total_mib": _parse_int(row[3]),
                    "temp_c": _parse_float(row[4]),
                }
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Unable to parse nvidia-smi output.") from exc

    if not gpus:
        raise ValueError("No GPU devices detected.")
    return gpus


def _aggregate_gpu_metrics(gpus: list[dict[str, Any]]) -> dict[str, Any]:
    gpu_count = len(gpus)
    mem_used = int(sum(int(g["mem_used_mib"]) for g in gpus))
    mem_total = int(sum(int(g["mem_total_mib"]) for g in gpus))
    util_avg = (
        round(sum(float(g["util_percent"]) for g in gpus) / gpu_count, 1) if gpu_count else 0.0
    )
    mem_used_percent = round((100.0 * mem_used / mem_total), 1) if mem_total > 0 else 0.0
    return {
        "gpu_count": gpu_count,
        "util_avg_percent": util_avg,
        "mem_used_total_mib": mem_used,
        "mem_total_mib": mem_total,
        "mem_used_percent": mem_used_percent,
    }


def _collect_bridge_metrics(
    bridge: BridgeProfile,
    *,
    tunnel_config: TunnelConfig,
    no_check: bool,
) -> dict[str, Any]:
    notebook_id = _bridge_notebook_id(bridge)
    result: dict[str, Any] = {
        "bridge": bridge.name,
        "notebook_id": notebook_id,
        "connected": False,
        "gpus": [],
        "aggregate": None,
        "error": None,
    }

    if not no_check:
        tunnel_ready = is_tunnel_available(
            bridge_name=bridge.name,
            config=tunnel_config,
            retries=0,
            retry_pause=0.0,
            progressive=False,
        )
        if not tunnel_ready:
            result["error"] = "SSH tunnel is not responding."
            return result

    try:
        completed = run_ssh_command(
            _NVIDIA_SMI_QUERY,
            bridge_name=bridge.name,
            config=tunnel_config,
            timeout=15,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        result["error"] = "Timed out while running nvidia-smi."
        return result
    except Exception as exc:  # pragma: no cover - defensive guard around subprocess path
        result["error"] = str(exc)
        return result

    if getattr(completed, "returncode", 1) != 0:
        stderr = (getattr(completed, "stderr", "") or "").strip()
        stdout = (getattr(completed, "stdout", "") or "").strip()
        reason = stderr or stdout or f"Remote command failed ({completed.returncode})."
        result["error"] = reason.splitlines()[0]
        return result

    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    try:
        gpus = _parse_nvidia_smi_output(stdout, stderr)
    except ValueError as exc:
        result["connected"] = True
        result["error"] = str(exc)
        return result

    result["connected"] = True
    result["gpus"] = gpus
    result["aggregate"] = _aggregate_gpu_metrics(gpus)
    return result


def _collect_all_metrics(
    bridges: list[BridgeProfile],
    *,
    tunnel_config: TunnelConfig,
    no_check: bool,
) -> list[dict[str, Any]]:
    if len(bridges) == 1:
        return [
            _collect_bridge_metrics(
                bridges[0],
                tunnel_config=tunnel_config,
                no_check=no_check,
            )
        ]

    results: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(bridges)) as pool:
        futures = {
            pool.submit(
                _collect_bridge_metrics,
                bridge,
                tunnel_config=tunnel_config,
                no_check=no_check,
            ): bridge.name
            for bridge in bridges
        }
        for future in concurrent.futures.as_completed(futures):
            bridge_name = futures[future]
            try:
                results[bridge_name] = future.result()
            except Exception as exc:  # pragma: no cover - defensive guard around thread pool
                bridge = next((b for b in bridges if b.name == bridge_name), None)
                results[bridge_name] = {
                    "bridge": bridge_name,
                    "notebook_id": _bridge_notebook_id(bridge) if bridge else "",
                    "connected": False,
                    "gpus": [],
                    "aggregate": None,
                    "error": str(exc),
                }

    return [results[bridge.name] for bridge in bridges if bridge.name in results]


def _build_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    ok = sum(1 for item in items if item.get("aggregate"))
    failed = len(items) - ok
    return {"total": len(items), "ok": ok, "failed": failed}


def _short_notebook_id(notebook_id: str) -> str:
    text = (notebook_id or "").strip()
    if not text:
        return "-"
    if len(text) <= 14:
        return text
    return f"{text[:11]}..."


def _format_human_output(payload: dict[str, Any]) -> str:
    lines = [
        "Notebook GPU Telemetry (tunnel-backed)",
        f"Sample: {payload['timestamp']}",
        "",
        f"{'Bridge':<22} {'Notebook':<15} {'GPUs':>4} {'Util':>8} {'Memory':>25} {'Status'}",
        "-" * 96,
    ]

    for item in payload["items"]:
        bridge = str(item.get("bridge", ""))
        notebook_id = _short_notebook_id(str(item.get("notebook_id", "")))
        aggregate = item.get("aggregate") or {}
        error = str(item.get("error", "") or "")

        if aggregate:
            gpus = int(aggregate.get("gpu_count", 0))
            util_avg = float(aggregate.get("util_avg_percent", 0.0))
            mem_used = int(aggregate.get("mem_used_total_mib", 0))
            mem_total = int(aggregate.get("mem_total_mib", 0))
            mem_pct = float(aggregate.get("mem_used_percent", 0.0))
            status = click.style("ok", fg="green")
            memory = f"{mem_used}/{mem_total} MiB ({mem_pct:.1f}%)"
            lines.append(
                f"{bridge:<22} {notebook_id:<15} {gpus:>4} {util_avg:>7.1f}% "
                f"{memory:>25} {status}"
            )
            continue

        status = click.style("error", fg="red")
        reason = error or "Unknown failure"
        lines.append(f"{bridge:<22} {notebook_id:<15} {0:>4} {'-':>8} {'-':>25} {status} {reason}")

    summary = payload["summary"]
    lines.extend(
        [
            "",
            f"Summary: {summary['ok']}/{summary['total']} bridge(s) collected successfully.",
        ]
    )
    return "\n".join(lines)


@click.command("top")
@click.option(
    "--bridge",
    "-b",
    help="Sample a specific notebook-backed bridge (defaults to all notebook-backed bridges).",
)
@click.option(
    "--watch",
    is_flag=True,
    help="Refresh continuously until interrupted.",
)
@click.option(
    "--interval",
    type=float,
    default=2.0,
    show_default=True,
    help="Refresh interval in seconds for --watch.",
)
@click.option(
    "--no-check",
    is_flag=True,
    help="Skip tunnel preflight checks and run nvidia-smi directly.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def notebook_top(
    ctx: Context,
    bridge: Optional[str],
    watch: bool,
    interval: float,
    no_check: bool,
    json_output: bool,
) -> None:
    """Show GPU utilization and memory for tunnel-backed notebooks."""
    json_output = resolve_json_output(ctx, json_output)

    if interval <= 0:
        _handle_error(
            ctx,
            "ValidationError",
            "--interval must be greater than 0.",
            EXIT_VALIDATION_ERROR,
        )
    if watch and json_output:
        _handle_error(
            ctx,
            "ValidationError",
            "--watch is not supported with --json.",
            EXIT_VALIDATION_ERROR,
            hint="Use a single sample with '--json' (without --watch).",
        )

    tunnel_config = load_tunnel_config()
    targets = _select_target_bridges(ctx, tunnel_config=tunnel_config, bridge_name=bridge)

    while True:
        payload = {
            "timestamp": _utc_timestamp(),
            "items": _collect_all_metrics(targets, tunnel_config=tunnel_config, no_check=no_check),
        }
        payload["summary"] = _build_summary(payload["items"])
        ok_count = int(payload["summary"]["ok"])

        if json_output:
            click.echo(json_formatter.format_json(payload, success=ok_count > 0))
            if ok_count == 0:
                raise SystemExit(EXIT_API_ERROR)
            return

        click.echo(_format_human_output(payload))
        if ok_count == 0 and not watch:
            _handle_error(
                ctx,
                "APIError",
                "Failed to collect GPU metrics from selected bridges.",
                EXIT_API_ERROR,
                hint="Run 'inspire notebook test <notebook>' for diagnostics.",
            )

        if not watch:
            return

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            return
        click.echo("")


__all__ = ["notebook_top"]
