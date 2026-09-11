"""Ray log windows and labels shared by CLI and SDK."""

from __future__ import annotations
from typing import Any
from inspire.services.ray.ray_instances import RayInstanceView
from inspire.platform.web.browser_api.ray_jobs import RAY_LOG_MAX_WINDOW_MS
from inspire.services.job.job_logs import web_log_time_range as _web_log_time_range

_WINDOW_PADDING_MS = 10 * 60 * 1000
_FALLBACK_WINDOW_MS = 24 * 60 * 60 * 1000


def clamped_window(
    detail: dict[str, Any],
    since_minutes: int | None,
) -> tuple[int, int, bool]:
    """Pick the query window, then hold it inside the platform's month cap."""
    # Apply the Ray cap here so the CLI can still report that it shortened the window.
    start_ms, end_ms = _web_log_time_range(detail, since_minutes, max_window_ms=None)
    clamped = end_ms - start_ms > RAY_LOG_MAX_WINDOW_MS
    if clamped:
        start_ms = end_ms - RAY_LOG_MAX_WINDOW_MS
    return start_ms, end_ms, clamped


def labelled_logs(
    logs: list[dict[str, Any]],
    views: list[RayInstanceView],
) -> list[dict[str, Any]]:
    """Swap each record's pod handle for the Agent-visible instance label.

    Relabelling before the budget runs — rather than at print time — keeps the
    character accounting measuring the string that is actually shown, and keeps
    the JSON schema identical to `job logs --json`.
    """
    labels = {view.handle: view.label for view in views}
    labels.update({view.handle.rsplit("/", 1)[-1]: view.label for view in views})
    relabelled: list[dict[str, Any]] = []
    for item in logs:
        row = dict(item)
        pod = str(row.get("pod_name") or "").strip()
        if pod in labels:
            row["pod_name"] = labels[pod]
        relabelled.append(row)
    return relabelled
