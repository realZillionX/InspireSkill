"""Platform log fetch and selection shared by CLI and SDK."""

from __future__ import annotations
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from inspire.platform.web.browser_api.jobs import JOB_LOG_MAX_WINDOW_MS

_SUBSECOND_RE = re.compile(r"\.(\d+)")


@dataclass(frozen=True)
class WebLogSelection:
    logs: list[dict[str, Any]]
    truncated: bool
    shown: int
    total: int
    limit: int | None
    character_limit: int | None
    shown_chars: int


def window_to_minutes(window: str) -> int:
    value = (window or "").strip().lower()
    if len(value) < 2:
        raise ValueError("use a window like 30m or 2h")
    unit = value[-1]
    try:
        amount = int(value[:-1])
    except ValueError as exc:
        raise ValueError("use a window like 30m or 2h") from exc
    if amount <= 0:
        raise ValueError("window must be positive")
    if unit == "m":
        return amount
    if unit == "h":
        return amount * 60
    if unit == "d":
        return amount * 24 * 60
    raise ValueError("window unit must be m, h, or d")


def coerce_epoch_ms(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def web_log_time_range(
    job_data: dict, since_minutes: int | None, *,
    max_window_ms: int | None = JOB_LOG_MAX_WINDOW_MS,
) -> tuple[int, int]:
    now_ms = int(time.time() * 1000)
    if since_minutes is not None:
        window_ms = since_minutes * 60 * 1000
        if max_window_ms is not None:
            window_ms = min(window_ms, max_window_ms)
        return now_ms - window_ms, now_ms

    created_ms = coerce_epoch_ms(job_data.get("created_at"))
    finished_ms = coerce_epoch_ms(job_data.get("finished_at"))
    if created_ms is None:
        return now_ms - 24 * 60 * 60 * 1000, now_ms

    start_ms = max(0, created_ms - 10 * 60 * 1000)
    end_ms = finished_ms + 10 * 60 * 1000 if finished_ms else now_ms
    end_ms = max(end_ms, start_ms + 1)
    if max_window_ms is not None:
        start_ms = max(start_ms, end_ms - max_window_ms)
    return start_ms, end_ms


def web_log_sub_ms(item: dict) -> int:
    """Return the sub-millisecond part of a record's `time`, in nanoseconds.

    The platform stamps `time` with nanosecond precision but rounds
    `timestamp_ms` to milliseconds, so a burst of lines written inside the
    same millisecond ties on `timestamp_ms` and comes back in whatever order
    the log store felt like. Ordering on the finer field keeps a job's stdout
    in the order the job actually wrote it — a CSV header before its row.
    """
    match = _SUBSECOND_RE.search(str(item.get("time") or ""))
    if not match:
        return 0
    digits = match.group(1)[:9].ljust(9, "0")
    return int(digits) % 1_000_000


def web_log_sort_key(item: dict) -> tuple[int, int, str]:
    timestamp_ms = coerce_epoch_ms(item.get("timestamp_ms")) or 0
    log_id = str(item.get("log_id") or "")
    return timestamp_ms, web_log_sub_ms(item), log_id


def web_log_identity(item: dict) -> tuple[int, str, str, int]:
    timestamp_ms = coerce_epoch_ms(item.get("timestamp_ms")) or 0
    log_id = str(item.get("log_id") or "")
    pod_name = str(item.get("pod_name") or "").strip()
    message = str(item.get("message") or item.get("log") or item.get("content") or "")
    return timestamp_ms, log_id, pod_name, hash(message)


def select_job_logs(
    logs: list[dict],
    *,
    total: int,
    tail: int | None,
    head: int | None,
    record_limit: int,
    all_output: bool,
    character_limit: int | None = None,
    project: Callable[[list[dict]], list[dict]] | None = None,
    formatter: Callable[[dict], str] | None = None,
    budget: Callable[..., tuple[list[dict], bool, int]] | None = None,
) -> WebLogSelection:
    formatter = formatter or format_log_line
    public_logs = sorted(logs, key=web_log_sort_key)
    if project is not None:
        public_logs = project(public_logs)
    normalized_total = max(int(total), len(public_logs))

    if all_output:
        selected = public_logs
        limit = None
        keep_tail = False
    elif head is not None:
        selected = public_logs[:head]
        limit = head
        keep_tail = False
    elif tail is not None:
        selected = public_logs[-tail:]
        limit = tail
        keep_tail = True
    else:
        selected = public_logs[-record_limit:]
        limit = record_limit
        keep_tail = True

    record_truncated = len(selected) < normalized_total
    if all_output or character_limit is None:
        budgeted = selected
        character_truncated = False
        shown_chars = sum(len(formatter(item)) for item in budgeted)
        character_limit = None
    else:
        if budget is None:
            raise ValueError("A character budget function is required.")
        budgeted, character_truncated, shown_chars = budget(
            selected,
            character_limit=character_limit,
            keep_tail=keep_tail,
        )

    return WebLogSelection(
        logs=budgeted,
        truncated=record_truncated or character_truncated,
        shown=len(budgeted),
        total=normalized_total,
        limit=limit,
        character_limit=character_limit,
        shown_chars=shown_chars,
    )


def format_log_line(item: dict) -> str:
    timestamp = str(item.get("timestamp_str") or item.get("time") or item.get("timestamp_ms") or "")
    pod = str(item.get("pod_name") or "")
    message = str(item.get("message") or item.get("log") or item.get("content") or "")
    return " ".join(part for part in (timestamp, pod, message) if part)


def fetch_job_logs(
    *,
    job_id: str,
    pod_names: list[str],
    start_ms: int,
    end_ms: int,
    limit: int = 100,
    all_output: bool = False,
    session: Any = None,
    fetch: Callable[..., tuple[list[dict], int]] | None = None,
) -> tuple[list[dict], int]:
    if fetch is None:
        from inspire.platform.web.browser_api.jobs import list_train_job_logs

        fetch = list_train_job_logs
    kwargs = dict(
        job_id=job_id,
        pod_names=pod_names,
        start_timestamp_ms=start_ms,
        end_timestamp_ms=end_ms,
        session=session,
    )
    logs, total = fetch(page_size=100 if all_output else limit, **kwargs)
    if all_output and total > len(logs):
        logs, total = fetch(page_size=total, **kwargs)
    return logs, total
