"""HPC log windows and labels shared by CLI and SDK."""

from __future__ import annotations
import time
from typing import Any
from inspire.services.hpc.hpc_instances import HPCInstanceView
from inspire.platform.web.browser_api.hpc_jobs import HPC_LOG_MAX_WINDOW_MS

_WINDOW_PADDING_MS = 10 * 60 * 1000
_FALLBACK_WINDOW_MS = 24 * 60 * 60 * 1000


def epoch_ms(value: object) -> int | None:
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    try:
        return int(text)
    except ValueError:
        return None


def log_time_range(
    instances: list[dict[str, Any]],
    since_minutes: int | None,
) -> tuple[int, int, bool]:
    """Pick the query window, then hold it inside the platform's month cap.

    Without `--window` the run's own lifetime is the right window, and the
    instance rows are where it lives: their `created_at` / `finished_at` are
    epoch-millisecond strings, while the job list renders both as formatted
    local time. A still-running pod reports an empty `finished_at`, which means
    "up to now" rather than "unknown".
    """
    now_ms = int(time.time() * 1000)
    if since_minutes is not None:
        start_ms, end_ms = now_ms - since_minutes * 60 * 1000, now_ms
    else:
        created = [
            value
            for value in (epoch_ms(inst.get("created_at")) for inst in instances)
            if value is not None
        ]
        finished = [epoch_ms(inst.get("finished_at")) for inst in instances]
        if not created:
            start_ms, end_ms = now_ms - _FALLBACK_WINDOW_MS, now_ms
        else:
            start_ms = min(created) - _WINDOW_PADDING_MS
            still_running = any(value is None for value in finished)
            latest = [value for value in finished if value is not None]
            end_ms = now_ms if still_running or not latest else max(latest) + _WINDOW_PADDING_MS

    start_ms = max(0, start_ms)
    end_ms = max(end_ms, start_ms + 1)
    clamped = end_ms - start_ms > HPC_LOG_MAX_WINDOW_MS
    if clamped:
        start_ms = end_ms - HPC_LOG_MAX_WINDOW_MS
    return start_ms, end_ms, clamped


def labelled_logs(
    logs: list[dict[str, Any]],
    views: list[HPCInstanceView],
) -> list[dict[str, Any]]:
    """Swap each record's pod handle for the Agent-visible instance label.

    The response carries the bare pod name; `scrub_raw_ids` reduces it to
    `<redacted>-cluster-slurmd-0`, which is noise in every line. Relabelling
    before the budget runs — rather than at print time — keeps the character
    accounting measuring the string that is actually shown, and keeps the JSON
    schema identical to `job logs --json`.
    """
    labels = {view.pod: view.label for view in views}
    relabelled: list[dict[str, Any]] = []
    for item in logs:
        row = dict(item)
        pod = str(row.get("pod_name") or "").strip()
        if pod in labels:
            row["pod_name"] = labels[pod]
        # The shared sort key is (timestamp_ms, log_id), and `timestamp_ms`
        # rounds away the sub-millisecond part of a burst — a Slurm task
        # writing six lines inside one millisecond then gets ordered by an
        # opaque id, which scrambles the program's own output. `timestamp_str`
        # keeps nanoseconds and compares correctly as text, so it is the right
        # tie-breaker. The id itself is never printed: the JSON sanitizer
        # drops it and the human line never read it.
        precise = str(row.get("timestamp_str") or "").strip()
        if precise:
            row["log_id"] = precise
        relabelled.append(row)
    return relabelled
