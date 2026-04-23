"""`inspire notebook metrics <id>` — resource-utilization time series.

Backs the web UI "资源视图" tab (``POST /api/v1/cluster_metric/resource_metric_by_time``).
No SSH tunnel or nvidia-smi — pulls history straight from the platform's
cluster-metric service. Aggregates across all pods of the instance and renders
a sparkline + min/max/avg/last per metric; ``--json`` emits raw per-pod series.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.config import ConfigError
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api.metrics import (
    INTERVAL_CHOICES,
    METRIC_TYPES,
    MetricGroup,
    TASK_TYPE_BY_RESOURCE,
    get_resource_metrics_by_time,
)
from inspire.platform.web.session import SessionExpiredError, get_web_session

from .notebook_metrics_plot import render_metrics_png

# ---------------------------------------------------------------------------
# Metric selection
# ---------------------------------------------------------------------------

_METRIC_ALIASES: dict[str, str] = {
    "gpu": "gpu_usage_rate",
    "gpu_mem": "gpu_memory_usage_rate",
    "gpu_memory": "gpu_memory_usage_rate",
    "cpu": "cpu_usage_rate",
    "mem": "memory_usage_rate",
    "memory": "memory_usage_rate",
    "disk_read": "disk_io_read",
    "disk_write": "disk_io_write",
    "net_read": "network_tcp_ip_io_read",
    "net_write": "network_tcp_ip_io_write",
}

_CORE_METRICS: tuple[str, ...] = (
    "gpu_usage_rate",
    "gpu_memory_usage_rate",
    "cpu_usage_rate",
    "memory_usage_rate",
)

# ---------------------------------------------------------------------------
# Window parsing
# ---------------------------------------------------------------------------

_WINDOW_RE = re.compile(r"^(\d+)\s*([smhd])$")
_WINDOW_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Unicode block chars used for the sparkline.
_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _parse_window(text: str) -> int:
    m = _WINDOW_RE.match(text.strip().lower())
    if not m:
        raise click.BadParameter(
            f"unrecognized window '{text}' — use e.g. 30m / 1h / 6h / 24h / 7d"
        )
    qty, unit = int(m.group(1)), m.group(2)
    return qty * _WINDOW_MULT[unit]


def _parse_absolute(text: str) -> int:
    text = text.strip()
    # Accept ISO-like strings or bare dates; assume UTC to match the rest of
    # the CLI (inspire hpc / job events print UTC ISO strings too).
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    # Accept raw unix seconds too.
    try:
        return int(text)
    except ValueError as exc:
        raise click.BadParameter(f"unrecognized timestamp '{text}'") from exc


def _resolve_metrics(selector: Optional[str]) -> list[str]:
    if not selector or selector.lower() == "core":
        return list(_CORE_METRICS)
    if selector.lower() == "all":
        return list(METRIC_TYPES)
    out: list[str] = []
    for token in selector.split(","):
        token = token.strip()
        if not token:
            continue
        normalized = _METRIC_ALIASES.get(token.lower(), token)
        if normalized not in METRIC_TYPES:
            raise click.BadParameter(
                f"unknown metric '{token}' — valid aliases: "
                f"{', '.join(sorted(_METRIC_ALIASES))} or raw: "
                f"{', '.join(METRIC_TYPES)}"
            )
        if normalized not in out:
            out.append(normalized)
    if not out:
        raise click.BadParameter("no metrics selected")
    return out


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _is_rate(metric: str) -> bool:
    return metric.endswith("_usage_rate")


def _fmt_value(metric: str, value: float) -> str:
    """Render a metric value in a unit that matches the UI."""
    if _is_rate(metric):
        # Backend returns a 0-1 ratio; UI displays percent.
        return f"{value * 100:.1f}%"
    # IO metrics are bytes/second.
    units = (
        ("B/s", 1.0),
        ("KiB/s", 1024.0),
        ("MiB/s", 1024.0 ** 2),
        ("GiB/s", 1024.0 ** 3),
    )
    last = units[0]
    for unit, div in units:
        if value >= div:
            last = (unit, div)
    return f"{value / last[1]:.2f} {last[0]}"


def _sparkline(values: list[float], width: int = 40) -> str:
    if not values:
        return ""
    step = max(1, len(values) // width)
    # Bucket down to `width` means with simple chunking.
    buckets: list[float] = []
    for i in range(0, len(values), step):
        chunk = values[i : i + step]
        buckets.append(sum(chunk) / len(chunk))
        if len(buckets) >= width:
            break
    if not buckets:
        return ""
    lo, hi = min(buckets), max(buckets)
    span = hi - lo
    n_chars = len(_SPARK_CHARS)
    if span <= 0:
        return _SPARK_CHARS[0] * len(buckets)
    out: list[str] = []
    for v in buckets:
        idx = int(round((v - lo) / span * (n_chars - 1)))
        idx = max(0, min(idx, n_chars - 1))
        out.append(_SPARK_CHARS[idx])
    return "".join(out)


def _iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "Z"


def _aggregate_by_metric(
    metrics: list[str], groups: list[MetricGroup]
) -> dict[str, list[MetricGroup]]:
    by_metric: dict[str, list[MetricGroup]] = {m: [] for m in metrics}
    for g in groups:
        by_metric.setdefault(g.metric_type, []).append(g)
    return by_metric


def _aggregate_series(metric_groups: list[MetricGroup]) -> tuple[list[int], list[float]]:
    """Average samples across pods per timestamp. Returns (sorted_timestamps, values)."""
    per_ts: dict[int, list[float]] = {}
    for g in metric_groups:
        for s in g.samples:
            per_ts.setdefault(s.timestamp, []).append(s.value)
    if not per_ts:
        return [], []
    timestamps = sorted(per_ts)
    values = [sum(per_ts[t]) / len(per_ts[t]) for t in timestamps]
    return timestamps, values


def _format_text_summary(
    *,
    notebook_id: str,
    logic_compute_group_id: str,
    start_ts: int,
    end_ts: int,
    interval_label: str,
    metrics: list[str],
    groups: list[MetricGroup],
    include_sparkline: bool,
    chart_path: Optional[Path],
) -> str:
    by_metric = _aggregate_by_metric(metrics, groups)
    pods = sorted({g.group_name for g in groups if g.group_name})

    lines: list[str] = []
    if chart_path is not None:
        lines.append(f"Chart: {chart_path}")
        lines.append("")

    lines.extend(
        [
            f"Notebook Metrics — {notebook_id}",
            f"LCG: {logic_compute_group_id}   Pods: {len(pods)}",
            f"Window: {_iso_utc(start_ts)} → {_iso_utc(end_ts)}   Interval: {interval_label}",
            "",
        ]
    )

    for metric in metrics:
        metric_groups = by_metric.get(metric, [])
        if not metric_groups:
            lines.append(f"{metric}   (no data)")
            if include_sparkline:
                lines.append("")
            continue

        timestamps, values = _aggregate_series(metric_groups)
        if not values:
            lines.append(f"{metric}   (no samples)")
            if include_sparkline:
                lines.append("")
            continue

        lo, hi = min(values), max(values)
        avg = sum(values) / len(values)
        last = values[-1]
        stats = (
            f"{metric}   pods={len(metric_groups)}   samples={len(timestamps)}   "
            f"min={_fmt_value(metric, lo)}   max={_fmt_value(metric, hi)}   "
            f"avg={_fmt_value(metric, avg)}   last={_fmt_value(metric, last)}"
        )
        lines.append(stats)
        if include_sparkline:
            lines.append(f"  {_sparkline(values)}")
            lines.append("")

    return "\n".join(lines).rstrip()


def _default_plot_path(notebook_id: str, end_ts: int) -> Path:
    base = Path(os.environ.get("INSPIRE_METRICS_DIR", "")).expanduser()
    if not base or str(base) == ".":
        base = Path.home() / ".inspire" / "metrics"
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", notebook_id)
    return base / f"{safe_id}-{end_ts}.png"


def _open_file(path: Path) -> None:
    """Open `path` in the platform's default viewer; silently no-op on failure."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])  # noqa: S603,S607
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607
        elif sys.platform == "win32":  # pragma: no cover - Windows support
            os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - best-effort
        pass


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def _resolve_lcg_from_detail(detail: dict) -> Optional[str]:
    """Pull logic_compute_group_id out of a notebook detail payload.

    The live field is ``start_config.logic_compute_group_id`` (verified
    2026-04 via `GET /api/v1/notebook/{id}`). The top-level
    ``logic_compute_group.*`` object exists but platform-side leaves its
    ID fields empty — keep a defensive fallback in case they populate it
    later.
    """
    if not isinstance(detail, dict):
        return None
    start_cfg = detail.get("start_config")
    if isinstance(start_cfg, dict):
        lcg = start_cfg.get("logic_compute_group_id")
        if isinstance(lcg, str) and lcg.strip():
            return lcg.strip()
    grp = detail.get("logic_compute_group")
    if isinstance(grp, dict):
        for key in ("logic_compute_group_id", "compute_group_id"):
            value = grp.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@click.command("metrics")
@click.argument("notebook_id")
@click.option(
    "--metric",
    "metric_selector",
    default=None,
    help=(
        "Metrics to query: 'core' (default — gpu, gpu_mem, cpu, mem), "
        "'all' (8 metrics), or comma-separated aliases/raw names. "
        "Aliases: gpu, gpu_mem, cpu, mem, disk_read, disk_write, net_read, net_write."
    ),
)
@click.option(
    "--window",
    default="1h",
    show_default=True,
    help="Lookback ending at --end (or now). Format: <qty><s|m|h|d>, e.g. 30m / 6h / 7d. Ignored when --start is given.",
)
@click.option(
    "--start",
    default=None,
    help="Absolute window start (ISO, 'YYYY-MM-DD HH:MM:SS', or unix seconds). UTC assumed.",
)
@click.option(
    "--end",
    default=None,
    help="Absolute window end (defaults to now).",
)
@click.option(
    "--interval",
    type=click.Choice(list(INTERVAL_CHOICES)),
    default="1m",
    show_default=True,
    help="Sample interval (matches UI selector options).",
)
@click.option(
    "--lcg",
    "logic_compute_group_id",
    default=None,
    help="Override logic_compute_group_id (auto-resolved from notebook detail by default).",
)
@click.option(
    "--plot",
    "plot_path",
    default=None,
    help=(
        "Write the PNG chart to this path instead of the default "
        "~/.inspire/metrics/<id>-<unix>.png (set INSPIRE_METRICS_DIR to move the default base)."
    ),
)
@click.option(
    "--no-plot",
    is_flag=True,
    help="Skip PNG rendering; emit only the text summary.",
)
@click.option(
    "--sparkline",
    "sparkline",
    is_flag=True,
    help="Add inline unicode sparklines under each metric's stats line.",
)
@click.option(
    "--open",
    "open_after",
    is_flag=True,
    help="Open the rendered PNG in the system default viewer after writing it.",
)
@click.option("--json", "json_output", is_flag=True, help="Alias for global --json.")
@pass_context
def notebook_metrics(
    ctx: Context,
    notebook_id: str,
    metric_selector: Optional[str],
    window: str,
    start: Optional[str],
    end: Optional[str],
    interval: str,
    logic_compute_group_id: Optional[str],
    plot_path: Optional[str],
    no_plot: bool,
    sparkline: bool,
    open_after: bool,
    json_output: bool,
) -> None:
    """Query historical GPU / CPU / memory / disk / network utilization.

    Backs the 资源视图 tab in the web UI. By default renders a PNG chart
    (same style as the web UI — one subplot per metric, blue line over
    time) to ``~/.inspire/metrics/<id>-<unix>.png`` and prints that path
    plus one-line stats per metric. Multimodal agents (Claude Code etc.)
    can Read the PNG directly; humans can `open` it.

    ``--json`` emits raw per-pod per-timestamp samples and skips the PNG.
    ``--no-plot`` keeps the text summary without generating an image.
    ``--sparkline`` adds a unicode bar chart under each stats line.

    \b
    Examples:
        inspire notebook metrics <id>                          # default: PNG + stats
        inspire notebook metrics <id> --open                   # PNG + auto-open
        inspire notebook metrics <id> --metric gpu,gpu_mem --window 30m
        inspire notebook metrics <id> --metric all --interval 5m --window 24h
        inspire notebook metrics <id> --no-plot --sparkline    # terminal-only view
        inspire --json notebook metrics <id> --window 6h       # raw time series
    """
    json_output = resolve_json_output(ctx, json_output)

    try:
        metrics = _resolve_metrics(metric_selector)
    except click.BadParameter as exc:
        _handle_error(ctx, "ValidationError", str(exc), EXIT_VALIDATION_ERROR)
        return

    now = int(time.time())
    try:
        if start:
            start_ts = _parse_absolute(start)
            end_ts = _parse_absolute(end) if end else now
        else:
            end_ts = _parse_absolute(end) if end else now
            start_ts = end_ts - _parse_window(window)
    except click.BadParameter as exc:
        _handle_error(ctx, "ValidationError", str(exc), EXIT_VALIDATION_ERROR)
        return

    if end_ts <= start_ts:
        _handle_error(
            ctx,
            "ValidationError",
            "end time must be after start time",
            EXIT_VALIDATION_ERROR,
        )
        return

    interval_s = INTERVAL_CHOICES[interval]

    try:
        session = get_web_session()

        lcg = logic_compute_group_id
        if not lcg:
            detail = browser_api_module.get_notebook_detail(
                notebook_id=notebook_id, session=session
            )
            lcg = _resolve_lcg_from_detail(detail)
        if not lcg:
            _handle_error(
                ctx,
                "ConfigError",
                "Unable to resolve logic_compute_group_id from notebook detail.",
                EXIT_CONFIG_ERROR,
                hint="Pass it explicitly with --lcg lcg-...",
            )
            return

        groups = get_resource_metrics_by_time(
            task_id=notebook_id,
            task_type=TASK_TYPE_BY_RESOURCE["notebook"],
            logic_compute_group_id=lcg,
            metric_types=metrics,
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            interval_second=interval_s,
            session=session,
        )
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
        return
    except SessionExpiredError as exc:
        _handle_error(ctx, "AuthenticationError", str(exc), EXIT_AUTH_ERROR)
        return
    except ValueError as exc:
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return

    if json_output:
        payload = {
            "notebook_id": notebook_id,
            "logic_compute_group_id": lcg,
            "task_type": TASK_TYPE_BY_RESOURCE["notebook"],
            "metric_types": metrics,
            "time_range": {
                "start_timestamp": start_ts,
                "end_timestamp": end_ts,
                "interval_second": interval_s,
            },
            "groups": [
                {
                    "group_name": g.group_name,
                    "metric_type": g.metric_type,
                    "resource_name": g.resource_name,
                    "time_series": [
                        {"timestamp": s.timestamp, "value": s.value} for s in g.samples
                    ],
                }
                for g in groups
            ],
        }
        click.echo(json_formatter.format_json(payload))
        return

    chart_path: Optional[Path] = None
    if not no_plot:
        target = Path(plot_path).expanduser() if plot_path else _default_plot_path(
            notebook_id, end_ts
        )
        try:
            chart_path = render_metrics_png(
                notebook_id=notebook_id,
                start_ts=start_ts,
                end_ts=end_ts,
                metrics=metrics,
                groups=groups,
                out_path=target,
            )
        except Exception as exc:  # pragma: no cover - matplotlib-side failures
            _handle_error(
                ctx,
                "APIError",
                f"Failed to render chart PNG: {exc}",
                EXIT_API_ERROR,
                hint="Rerun with --no-plot for the text summary; report the trace if reproducible.",
            )
            return

    click.echo(
        _format_text_summary(
            notebook_id=notebook_id,
            logic_compute_group_id=lcg,
            start_ts=start_ts,
            end_ts=end_ts,
            interval_label=interval,
            metrics=metrics,
            groups=groups,
            include_sparkline=sparkline,
            chart_path=chart_path,
        )
    )

    if chart_path is not None and open_after:
        _open_file(chart_path)


__all__ = ["notebook_metrics"]
