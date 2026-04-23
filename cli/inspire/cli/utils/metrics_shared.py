"""Shared core for `inspire <notebook|job|hpc|serving> metrics <id>` commands.

All four resource types hit the same Browser-API endpoint
(``cluster_metric/resource_metric_by_time``) with a different ``task_type``
discriminator. Factoring the flag stack, time-window parsing, formatting
and PNG rendering here keeps each per-resource command a thin wrapper that
only contributes:

- the Click command name registered under its resource group
- a ``lcg_resolver`` callable (where does `logic_compute_group_id` live in
  this resource's detail endpoint) so ``--lcg`` stays optional
- the CLI's display label and positional-arg help text

Multi-pod rendering is the primary motivation: ``inspire job metrics`` on a
distributed-training run with N workers needs to surface per-worker divergence
(one worker stuck at 0% while the rest are at 95% is the signal), and that
logic lives entirely in the shared renderer.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

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
from inspire.platform.web.browser_api.metrics import (
    INTERVAL_CHOICES,
    METRIC_TYPES,
    MetricGroup,
    TASK_TYPE_BY_RESOURCE,
    get_resource_metrics_by_time,
)
from inspire.platform.web.session import SessionExpiredError, WebSession, get_web_session

from inspire.cli.utils.metrics_plot import render_metrics_png

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
# Time-window parsing
# ---------------------------------------------------------------------------

_WINDOW_RE = re.compile(r"^(\d+)\s*([smhd])$")
_WINDOW_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400}

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
    if _is_rate(metric):
        return f"{value * 100:.1f}%"
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
    return "".join(
        _SPARK_CHARS[max(0, min(int(round((v - lo) / span * (n_chars - 1))), n_chars - 1))]
        for v in buckets
    )


def _iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "Z"


def _aggregate_by_metric(
    metrics: list[str], groups: list[MetricGroup]
) -> dict[str, list[MetricGroup]]:
    by_metric: dict[str, list[MetricGroup]] = {m: [] for m in metrics}
    for g in groups:
        by_metric.setdefault(g.metric_type, []).append(g)
    return by_metric


def _flatten_values(metric_groups: list[MetricGroup]) -> list[float]:
    out: list[float] = []
    for g in metric_groups:
        out.extend(s.value for s in g.samples)
    return out


def _per_pod_last(metric_groups: list[MetricGroup]) -> list[tuple[str, float]]:
    """Per-pod last sample value; empty series contribute nothing."""
    out: list[tuple[str, float]] = []
    for g in metric_groups:
        if g.samples:
            out.append((g.group_name, g.samples[-1].value))
    return out


def _format_text_summary(
    *,
    resource_label: str,
    task_id: str,
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
            f"{resource_label} Metrics — {task_id}",
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

        values = _flatten_values(metric_groups)
        if not values:
            lines.append(f"{metric}   (no samples)")
            if include_sparkline:
                lines.append("")
            continue

        lo, hi = min(values), max(values)
        avg = sum(values) / len(values)
        # Aggregate "last" across pods: average of each pod's final sample.
        per_pod = _per_pod_last(metric_groups)
        last_vals = [v for _, v in per_pod]
        last_avg = sum(last_vals) / len(last_vals) if last_vals else 0.0

        header = (
            f"{metric}   pods={len(metric_groups)}   samples={len(values)}   "
            f"min={_fmt_value(metric, lo)}   max={_fmt_value(metric, hi)}   "
            f"avg={_fmt_value(metric, avg)}   last-avg={_fmt_value(metric, last_avg)}"
        )
        lines.append(header)

        # Stragglers detection: if pod count > 1, surface the worst/best last
        # values — catches "7 workers at 95%, 1 worker at 0%" at a glance.
        if len(per_pod) > 1:
            worst_name, worst_val = min(per_pod, key=lambda kv: kv[1])
            best_name, best_val = max(per_pod, key=lambda kv: kv[1])
            spread = best_val - worst_val
            lines.append(
                f"  last-min={_fmt_value(metric, worst_val)} ({_short_pod(worst_name)})   "
                f"last-max={_fmt_value(metric, best_val)} ({_short_pod(best_name)})   "
                f"spread={_fmt_value(metric, spread)}"
            )

        if include_sparkline:
            # Sparkline uses timestamp-averaged series for the overall shape.
            per_ts: dict[int, list[float]] = {}
            for g in metric_groups:
                for s in g.samples:
                    per_ts.setdefault(s.timestamp, []).append(s.value)
            timestamps = sorted(per_ts)
            spark_vals = [sum(per_ts[t]) / len(per_ts[t]) for t in timestamps]
            lines.append(f"  {_sparkline(spark_vals)}")
            lines.append("")

    return "\n".join(lines).rstrip()


def _short_pod(name: str) -> str:
    """Mirror the plot-side label shortening so text summary stays compact."""
    if not name:
        return "?"
    for marker in ("-worker-", "-replica-", "-master-", "-launcher-"):
        idx = name.rfind(marker)
        if idx >= 0:
            return name[idx + 1 :]
    return name if len(name) <= 28 else name[:25] + "…"


# ---------------------------------------------------------------------------
# Output-path & open helpers
# ---------------------------------------------------------------------------


def _default_plot_path(resource_name: str, task_id: str, end_ts: int) -> Path:
    base = Path(os.environ.get("INSPIRE_METRICS_DIR", "")).expanduser()
    if not base or str(base) == ".":
        base = Path.home() / ".inspire" / "metrics"
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    return base / f"{resource_name}-{safe_id}-{end_ts}.png"


def _open_file(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])  # noqa: S603,S607
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607
        elif sys.platform == "win32":  # pragma: no cover
            os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# LCG-resolver type
# ---------------------------------------------------------------------------

# Signature: (task_id, session) -> lcg | None. Implementations may issue
# additional HTTP calls (detail endpoints) to locate the field.
LcgResolver = Callable[[str, WebSession], Optional[str]]


# ---------------------------------------------------------------------------
# Command factory
# ---------------------------------------------------------------------------


def build_metrics_command(
    *,
    resource_name: str,
    resource_label: str,
    id_arg: str,
    id_help: str,
    lcg_resolver: LcgResolver,
) -> click.Command:
    """Return a Click command that queries metrics for one task type.

    Each resource module (notebook / job / hpc / serving) calls this with
    its own ``lcg_resolver`` and registers the returned command under its
    own group as `metrics`.
    """

    task_type = TASK_TYPE_BY_RESOURCE[resource_name]

    @click.command("metrics")
    @click.argument(id_arg)
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
        help="Override logic_compute_group_id (auto-resolved from detail endpoint by default).",
    )
    @click.option(
        "--plot",
        "plot_path",
        default=None,
        help=(
            "Write the PNG chart to this path instead of the default "
            "~/.inspire/metrics/<resource>-<id>-<unix>.png "
            "(set INSPIRE_METRICS_DIR to move the default base)."
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
        help="Add an inline unicode sparkline under each metric's stats line.",
    )
    @click.option(
        "--open",
        "open_after",
        is_flag=True,
        help="Open the rendered PNG in the system default viewer after writing it.",
    )
    @click.option("--json", "json_output", is_flag=True, help="Alias for global --json.")
    @pass_context
    def metrics_cmd(
        ctx: Context,
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
        **kwargs: Any,
    ) -> None:
        """Query historical GPU / CPU / memory / disk / network utilization.

        Backs the 资源视图 tab in the web UI. For multi-pod workloads
        (distributed training, multi-replica serving) every pod is drawn
        as its own line — divergence between workers is the signal you're
        watching for when monitoring "is training still healthy?".
        """
        task_id = kwargs[id_arg]

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
                lcg = lcg_resolver(task_id, session)
            if not lcg:
                _handle_error(
                    ctx,
                    "ConfigError",
                    f"Unable to resolve logic_compute_group_id for {resource_name} '{task_id}'.",
                    EXIT_CONFIG_ERROR,
                    hint=(
                        "Auto-resolution reads logic_compute_group_id from the resource's "
                        "detail endpoint — make sure the resource exists and you have access. "
                        "The --lcg escape hatch takes a raw id for debugging only."
                    ),
                )
                return

            groups = get_resource_metrics_by_time(
                task_id=task_id,
                task_type=task_type,
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
                "resource": resource_name,
                f"{resource_name}_id": task_id,
                "logic_compute_group_id": lcg,
                "task_type": task_type,
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
            target = (
                Path(plot_path).expanduser()
                if plot_path
                else _default_plot_path(resource_name, task_id, end_ts)
            )
            try:
                chart_path = render_metrics_png(
                    task_id=task_id,
                    task_label=resource_label,
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
                resource_label=resource_label,
                task_id=task_id,
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

    metrics_cmd.params[0].help = id_help  # attach arg help once positional is built
    return metrics_cmd


__all__ = ["LcgResolver", "build_metrics_command"]
