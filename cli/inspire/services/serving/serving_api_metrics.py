from __future__ import annotations

from typing import Any, Optional
from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.platform.web.browser_api.servings import SERVING_API_METRIC_TYPES

_CORE_API_METRICS: tuple[str, ...] = ("QPS", "SUCCESS_RATE", "LATENCY")

_API_METRIC_ALIASES: dict[str, str] = {
    "qps": "QPS",
    "success_qps": "SUCCESS_QPS",
    "fail_qps": "FAIL_QPS",
    "success_rate": "SUCCESS_RATE",
    "fail_rate": "FAIL_RATE",
    "requests": "REQUEST_COUNT",
    "latency": "LATENCY",
    "ttft": "TTFT",
    "ttlt": "TTLT",
    "input_tokens": "INPUT_TOKENS",
    "output_tokens": "OUTPUT_TOKENS",
}

_WINDOW_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_window(text: str) -> int:
    raw = text.strip().lower()
    if len(raw) < 2 or raw[-1] not in _WINDOW_MULT or not raw[:-1].isdigit():
        raise ValueError(f"unrecognized window '{text}' — use e.g. 30m / 1h / 6h / 24h / 7d")
    return int(raw[:-1]) * _WINDOW_MULT[raw[-1]]


def resolve_api_metrics(selector: Optional[str]) -> list[str]:
    if not selector or selector.strip().lower() == "core":
        return list(_CORE_API_METRICS)
    if selector.strip().lower() == "all":
        return list(SERVING_API_METRIC_TYPES)
    out: list[str] = []
    for token in selector.split(","):
        token = token.strip()
        if not token:
            continue
        normalized = _API_METRIC_ALIASES.get(token.lower(), token.upper())
        if normalized not in SERVING_API_METRIC_TYPES:
            raise ValueError(
                f"unknown serving API metric '{token}' — valid aliases: "
                f"{', '.join(sorted(_API_METRIC_ALIASES))} or raw: "
                f"{', '.join(SERVING_API_METRIC_TYPES)}"
            )
        if normalized not in out:
            out.append(normalized)
    if not out:
        raise ValueError("no metrics selected")
    return out


def samples(group: dict[str, Any]) -> list[float]:
    series = group.get("time_series")
    if not isinstance(series, list):
        return []
    values: list[float] = []
    for row in series:
        if not isinstance(row, dict):
            continue
        try:
            values.append(float(row.get("data", 0)))
        except (TypeError, ValueError):
            continue
    return values


def group_summary(group: dict[str, Any]) -> dict[str, Any]:
    values = samples(group)
    summary: dict[str, Any] = {
        "metric": str(group.get("metric_type") or ""),
        "count": len(values),
    }
    unit = str(group.get("data_unit") or "").strip()
    if unit:
        summary["unit"] = unit
    label = scrub_raw_ids(str(group.get("group_name") or "")).strip()
    if label and "<redacted>" not in label:
        summary["group"] = label
    if values:
        summary.update(
            {
                "min": min(values),
                "max": max(values),
                "avg": sum(values) / len(values),
                "last": values[-1],
                "total": sum(values),
            }
        )
    return summary
