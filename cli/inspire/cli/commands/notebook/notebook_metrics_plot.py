"""Render metric time-series into a PNG chart matching the web UI's 资源视图.

Multimodal agents (Claude Code, Codex, Gemini CLI, …) can read the emitted
PNG directly via their image-capable Read tool, so keeping the visual output
compact and chart-style is the cheapest way to give them the same signal a
human reads off the web chart. See notebook_metrics.py for the CLI wiring.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")  # no display server needed

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from inspire.platform.web.browser_api.metrics import MetricGroup  # noqa: E402

# Metric → human-readable subplot title. English avoids font-fallback glyphs
# on matplotlib defaults; agents read either language equally well.
_METRIC_TITLES: dict[str, str] = {
    "gpu_usage_rate": "GPU Utilization",
    "gpu_memory_usage_rate": "GPU Memory Utilization",
    "cpu_usage_rate": "CPU Utilization",
    "memory_usage_rate": "Memory Utilization",
    "disk_io_read": "Disk I/O Read",
    "disk_io_write": "Disk I/O Write",
    "network_tcp_ip_io_read": "Network TCP Read",
    "network_tcp_ip_io_write": "Network TCP Write",
}

_POD_COLORS = (
    "#4e8ef5",  # primary blue (matches web UI)
    "#f5a623",  # orange
    "#23b889",  # green
    "#d94a5c",  # red
    "#8e5ee8",  # purple
    "#2cb5c0",  # teal
)


def _is_rate(metric: str) -> bool:
    return metric.endswith("_usage_rate")


def _format_bytes_per_sec(val: float, _pos: int | None = None) -> str:
    if val <= 0:
        return "0"
    units = (("GiB/s", 1024**3), ("MiB/s", 1024**2), ("KiB/s", 1024), ("B/s", 1))
    for label, div in units:
        if val >= div:
            return f"{val / div:.1f} {label}"
    return f"{val:.0f} B/s"


def _format_percent(val: float, _pos: int | None = None) -> str:
    return f"{val * 100:.0f}%"


def _short_id(notebook_id: str) -> str:
    # ``91fbc44e-9c40-4c99-99f4-d27d6303266e`` → ``91fbc44e``
    cleaned = notebook_id.strip()
    return cleaned.split("-", 1)[0] if "-" in cleaned else cleaned[:8]


def render_metrics_png(
    *,
    notebook_id: str,
    start_ts: int,
    end_ts: int,
    metrics: list[str],
    groups: list[MetricGroup],
    out_path: Path,
) -> Path:
    """Render one subplot per requested metric into a single PNG.

    Layout mirrors the web UI's 资源视图 tab: a 2-column grid with one
    line chart per metric, smooth blue line, horizontal gridlines, y-axis
    as `0%–100%` for rates and human-readable `bytes/s` for I/O, x-axis
    as local-time `MM/DD HH:MM` matching the web display.

    Multi-pod groups (distributed training / multi-replica serving) draw
    one line per pod with a compact legend.
    """
    by_metric: dict[str, list[MetricGroup]] = {m: [] for m in metrics}
    for g in groups:
        by_metric.setdefault(g.metric_type, []).append(g)

    n = len(metrics)
    cols = 2 if n >= 2 else 1
    rows = math.ceil(n / cols)

    fig, axes_grid = plt.subplots(
        rows,
        cols,
        figsize=(7 * cols, 3 * rows),
        squeeze=False,
    )
    axes = [ax for row in axes_grid for ax in row]

    start_dt = datetime.fromtimestamp(start_ts)  # local tz for axis labels
    end_dt = datetime.fromtimestamp(end_ts)

    fig.suptitle(
        f"Notebook {_short_id(notebook_id)}  ·  "
        f"{start_dt.strftime('%Y-%m-%d %H:%M')} → {end_dt.strftime('%Y-%m-%d %H:%M')}",
        fontsize=12,
        y=0.995,
    )

    for idx, metric in enumerate(metrics):
        ax = axes[idx]
        metric_groups = by_metric.get(metric, [])
        ax.set_title(_METRIC_TITLES.get(metric, metric), fontsize=11, loc="left")
        ax.set_xlim(start_dt, end_dt)

        # Cosmetics matching the web look.
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_visible(False)
        ax.tick_params(axis="y", length=0)
        ax.grid(axis="y", linestyle="-", linewidth=0.5, color="#e7ebf0")
        ax.set_axisbelow(True)

        if not metric_groups:
            ax.text(
                0.5,
                0.5,
                "(no data)",
                transform=ax.transAxes,
                ha="center",
                va="center",
                fontsize=10,
                color="#97a1b0",
            )
            ax.set_xticks([])
            ax.set_yticks([])
            continue

        has_samples = False
        for pod_idx, g in enumerate(metric_groups):
            if not g.samples:
                continue
            has_samples = True
            xs = [datetime.fromtimestamp(s.timestamp) for s in g.samples]
            ys = [s.value for s in g.samples]
            color = _POD_COLORS[pod_idx % len(_POD_COLORS)]
            ax.plot(
                xs,
                ys,
                color=color,
                linewidth=1.8,
                label=g.group_name if len(metric_groups) > 1 else None,
            )

        if not has_samples:
            ax.text(
                0.5,
                0.5,
                "(no samples)",
                transform=ax.transAxes,
                ha="center",
                va="center",
                fontsize=10,
                color="#97a1b0",
            )
            continue

        # Y-axis: percent for rates, human bytes/s for I/O.
        if _is_rate(metric):
            ax.set_ylim(0, 1.0)
            ax.yaxis.set_major_formatter(plt.FuncFormatter(_format_percent))
            ax.yaxis.set_major_locator(plt.MultipleLocator(0.2))
        else:
            ax.set_ylim(bottom=0)
            ax.yaxis.set_major_formatter(plt.FuncFormatter(_format_bytes_per_sec))

        # X-axis: time ticks like "04/22 23:34".
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=6))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))
        for tick in ax.get_xticklabels():
            tick.set_fontsize(9)

        if len(metric_groups) > 1:
            ax.legend(fontsize=8, loc="upper left", frameon=False)

    # Hide leftover axes if metrics count is odd.
    for j in range(n, len(axes)):
        axes[j].set_visible(False)

    fig.tight_layout(rect=[0, 0, 1, 0.985])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_path


__all__ = ["render_metrics_png"]
