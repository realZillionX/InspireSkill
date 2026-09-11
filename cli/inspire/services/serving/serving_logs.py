"""Pod-scoped serving log retrieval shared by CLI and SDK."""

from __future__ import annotations

import time
from typing import Any, Callable
from inspire.platform.web.session import WebSession
from inspire.services.serving.serving_instances import ServingInstanceView
from inspire.platform.web import browser_api as api
from inspire.services.serving.serving_instances import serving_instance_views, select_serving_instance_views


def log_window(minutes: int | None = None) -> tuple[int, int]:
    end = int(time.time() * 1000)
    return max(0, end - (minutes if minutes is not None else 1440) * 60_000), end


def fetch_logs(
    serving_id,
    *,
    session,
    selectors=(),
    start_ms=None,
    end_ms=None,
    since_minutes=None,
    fetch_size=100,
    all_output=False,
):
    rows, _ = api.list_serving_instances(serving_id, page=1, page_size=200, session=session)
    views = select_serving_instance_views(serving_instance_views(rows), selectors)
    pods = [v.handle for v in views]
    if not pods:
        return [], 0, pods
    if start_ms is None or end_ms is None:
        start_ms, end_ms = log_window(since_minutes)
    kwargs = dict(
        pod_names=pods,
        start_timestamp_ms=start_ms,
        end_timestamp_ms=end_ms,
        inference_serving_id=serving_id,
        session=session,
    )
    logs, total = api.list_serving_logs(**kwargs, page_size=100 if all_output else fetch_size)
    if all_output and total > len(logs):
        logs, total = api.list_serving_logs(**kwargs, page_size=total)
    return logs, total, pods


def list_serving_logs(
    serving_id: str, *, pod_names: list[str], start_timestamp_ms: int | str,
    end_timestamp_ms: int | str, page_size: int = 200, session: WebSession | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Adapt the positional workload identity to the serving log filter."""
    return api.list_serving_logs(
        inference_serving_id=serving_id, pod_names=pod_names,
        start_timestamp_ms=start_timestamp_ms, end_timestamp_ms=end_timestamp_ms,
        page_size=page_size, session=session,
    )


def labelled_logs(
    rows: list[dict[str, Any]], views: list[ServingInstanceView],
) -> list[dict[str, Any]]:
    """Serving log rows already carry the platform's pod labels."""
    return rows


def workload_log_window(
    rows: list[dict[str, Any]], detail: Callable[[], dict[str, Any]], minutes: int | None,
) -> tuple[int, int]:
    """Serving log windows are independent of workload creation timestamps."""
    return log_window(minutes)
