"""Render metric time-series into a PNG chart matching the web UI's 资源视图.

Multimodal agents (Claude Code, Codex, Gemini CLI, …) can read the emitted
PNG directly via their image-capable Read tool, so keeping the visual output
compact and chart-style is the cheapest way to give them the same signal a
human reads off the web chart. See `notebook_metrics.py` (and the parallel
thin wrappers under job/ hpc/ serving/) for the CLI wiring.

Multi-pod workloads (distributed training with N workers, multi-replica
servings) render one line per pod with distinct colors so divergence —
the core signal when monitoring "is every worker pulling its weight?" —
is visible at a glance. Palette is large enough for the common training
shapes (≤16 pods); beyond that colors cycle and the in-plot note still
shows the pod count.
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

# 12-color palette tuned for line distinguishability on white background.
# Cycled when pod count exceeds length (rare beyond 16-worker jobs).
_POD_COLORS = (
    "#4e8ef5",  # blue (matches web UI primary)
    "#f5a623",  # orange
    "#23b889",  # green
    "#d94a5c",  # red
    "#8e5ee8",  # purple
    "#2cb5c0",  # teal
    "#e2778d",  # pink
    "#6a98ff",  # light blue
    "#f5c523",  # yellow
    "#1f6e4f",  # dark green
    "#b3541e",  # brown
    "#6c757d",  # gray
)


def _short_pod_label(name: str) -> str:
    """Collapse long pod names into a legend-friendly worker / replica tag.

    Examples (upstream format verified 2026-04):
    - `job-<uuid>-worker-3`                      → `worker-3`
    - `alphazero-sglang--d27d6303266e-odwuujlhhz` → `alphazero-sglang-…`
    - `<svc>-replica-2`                           → `replica-2`
    - anything else                               → last 24 chars
    """
    if not name:
        return "?"
    for marker in ("-worker-", "-replica-", "-master-", "-launcher-"):
        idx = name.rfind(marker)
        if idx >= 0:
            return name[idx + 1 :]
    if len(name) <= 28:
        return name
    return name[:25] + "…"


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


# Known resource-type prefixes on task IDs. Longer prefixes must come first
# so ``hpc-job-...`` is not shortened to ``job-...`` by the ``hpc-`` rule.
_ID_PREFIXES = ("hpc-job-", "job-", "sv-", "hpc-", "nb-")


def _short_id(task_id: str) -> str:
    """Shorten a task id to a hash-length tag, stripping known type prefixes.

    - ``job-a211cbef-c30f-4602-...`` → ``a211cbef`` (strip ``job-`` first;
      a naive ``split('-',1)`` would return literal ``"job"``, producing
      titles like "Train Job job" in the subplot header)
    - ``hpc-job-xxxx-...``           → ``xxxx``
    - ``91fbc44e-9c40-...``          → ``91fbc44e`` (bare UUID path)
    """
    cleaned = task_id.strip()
    for prefix in _ID_PREFIXES:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    if "-" in cleaned:
        return cleaned.split("-", 1)[0]
    return cleaned[:8] if len(cleaned) > 8 else cleaned


def render_metrics_png(
    *,
    task_id: str,
    task_label: str,
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
    one line per pod with distinct colors. The legend is placed below
    the subplot (as a horizontal strip) once there are >3 pods so the
    data area stays unobstructed — critical when you're eyeballing 8
    workers for divergence.
    """
    by_metric: dict[str, list[MetricGroup]] = {m: [] for m in metrics}
    for g in groups:
        by_metric.setdefault(g.metric_type, []).append(g)

    n = len(metrics)
    cols = 2 if n >= 2 else 1
    rows = math.ceil(n / cols)

    # Give rows more vertical space when most subplots need a bottom legend.
    max_pods = max((len(by_metric.get(m, [])) for m in metrics), default=1)
    extra = 0.6 if max_pods > 3 else 0.0

    fig, axes_grid = plt.subplots(
        rows,
        cols,
        figsize=(7 * cols, (3 + extra) * rows),
        squeeze=False,
    )
    axes = [ax for row in axes_grid for ax in row]

    start_dt = datetime.fromtimestamp(start_ts)  # local tz for axis labels
    end_dt = datetime.fromtimestamp(end_ts)

    pod_count_for_title = max_pods
    pod_hint = f"  ·  {pod_count_for_title} pods" if pod_count_for_title > 1 else ""
    fig.suptitle(
        f"{task_label} {_short_id(task_id)}{pod_hint}  ·  "
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

        # Stable ordering so the same pod keeps the same color across subplots.
        metric_groups = sorted(metric_groups, key=lambda g: g.group_name)

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
                linewidth=1.6 if len(metric_groups) > 1 else 1.9,
                alpha=0.9,
                label=_short_pod_label(g.group_name) if len(metric_groups) > 1 else None,
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
            # Horizontal legend below the axes keeps the data region clean for
            # multi-worker jobs. Column count adapts to pod count so labels
            # fit without overlap.
            ncol = min(max(2, (len(metric_groups) + 1) // 2), 4)
            ax.legend(
                fontsize=8,
                loc="upper center",
                bbox_to_anchor=(0.5, -0.2),
                ncol=ncol,
                frameon=False,
                handlelength=1.5,
                columnspacing=1.2,
                borderaxespad=0.0,
            )

    # Hide leftover axes if metrics count is odd.
    for j in range(n, len(axes)):
        axes[j].set_visible(False)

    fig.tight_layout(rect=[0, 0, 1, 0.985])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_path


__all__ = ["render_metrics_png"]
