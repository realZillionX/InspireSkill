"""Metric selection and time parsing without CLI dependencies."""

from __future__ import annotations
import re
from datetime import datetime, timezone
from typing import Optional
from inspire.platform.web.browser_api.metrics import METRIC_TYPES, MetricGroup as MetricGroup

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


def parse_window(text: str) -> int:
    m = _WINDOW_RE.match(text.strip().lower())
    if not m:
        raise ValueError(f"unrecognized window '{text}' — use e.g. 30m / 1h / 6h / 24h / 7d")
    qty, unit = int(m.group(1)), m.group(2)
    return qty * _WINDOW_MULT[unit]


def parse_absolute(text: str) -> int:
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
        raise ValueError(f"unrecognized timestamp '{text}'") from exc


def resolve_metrics(selector: Optional[str]) -> list[str]:
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
            raise ValueError(
                f"unknown metric '{token}' — valid aliases: "
                f"{', '.join(sorted(_METRIC_ALIASES))} or raw: "
                f"{', '.join(METRIC_TYPES)}"
            )
        if normalized not in out:
            out.append(normalized)
    if not out:
        raise ValueError("no metrics selected")
    return out


def metric_group(detail: object) -> str | None:
    if isinstance(detail, dict):
        for key in ("logic_compute_group_id", "compute_group_id"):
            value = detail.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in detail.values():
            found = metric_group(value)
            if found:
                return found
    elif isinstance(detail, list):
        for value in detail:
            found = metric_group(value)
            if found:
                return found
    return None
