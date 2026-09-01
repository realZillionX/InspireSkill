"""Shared helpers for workload event commands.

Platform event records share most fields — `reason`, `message`, `from`,
`first_timestamp`, `last_timestamp`, `age`, `object_id`, `object_type` —
but not all. Some workload kinds carry a Kubernetes-style `type` (`Normal` /
`Warning`); others do not. Event history can be lossy after GC, so `[]` is a
normal steady state for long-completed workloads.

Events are always fetched from the live platform API. Local caches are not a
source of truth for user-visible diagnostics.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import click

from inspire.cli.context import Context, EXIT_API_ERROR
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.collection_output import BoundedCollection, truncation_notice
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.raw_ids import scrub_raw_ids

DEFAULT_EVENT_TAIL = 20
FOLLOW_EVENT_KEY_LIMIT = 2048


def _fmt_timestamp(raw: Any) -> str:
    """Events carry millisecond-epoch strings; fall back to raw string otherwise."""
    if raw is None:
        return "-"
    s = str(raw)
    if s.isdigit():
        try:
            value = int(s)
            # heuristic: values in ms range vs s range
            if value > 10**12:
                ts = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
            else:
                ts = datetime.fromtimestamp(value, tz=timezone.utc)
            return ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError):
            pass
    return s


def event_sort_key(event: dict) -> tuple[int, int, int]:
    """Order a merged event stream oldest-first.

    Controller-level and per-pod events come from different calls (and, on
    HPC, one call per instance), so the chronology that makes ``--tail`` mean
    "most recent" has to be imposed here rather than trusted from the
    platform's own ordering.

    Timestamps are per-second, so a container's `Pulled` / `Created` /
    `Started` trio usually shares one — hence the ``id`` tiebreaker, which Ray
    fills with a monotonic counter. Without it the causal order of a same-second
    burst flips depending on how the rows were fetched.
    """

    def _epoch(value: object) -> int:
        text = str(value or "").strip()
        return int(text) if text.isdigit() else 0

    return (
        _epoch(event.get("last_timestamp")),
        _epoch(event.get("first_timestamp")),
        _epoch(event.get("id")),
    )


def event_type(event: dict) -> str:
    """Read the Normal / Warning field under either spelling.

    Node events call it ``event_type``; every workload Action calls it
    ``type``. The difference has to be absorbed in one place, or `--type
    Warning` silently empties the node stream instead of filtering it.
    """
    return str(event.get("type") or event.get("event_type") or "")


def _matching_events(
    events: list[dict],
    *,
    type_filter: Optional[str] = None,
    reason_filter: Optional[str] = None,
    keyword_filter: Optional[str] = None,
) -> list[dict]:
    """Apply event filters without imposing an output window."""
    out = events
    if type_filter:
        needle = type_filter.lower()
        out = [e for e in out if event_type(e).lower() == needle]
    if reason_filter:
        needle = reason_filter.lower()
        out = [e for e in out if needle in str(e.get("reason", "")).lower()]
    if keyword_filter:
        needle = keyword_filter.lower()
        out = [
            e
            for e in out
            if needle in event_type(e).lower()
            or any(
                needle in str(e.get(key, "")).lower()
                for key in ("reason", "message", "from", "content")
            )
        ]
    return out


def _event_window(
    events: list[dict],
    *,
    type_filter: Optional[str] = None,
    reason_filter: Optional[str] = None,
    keyword_filter: Optional[str] = None,
    tail: Optional[int] = None,
) -> BoundedCollection[dict]:
    matching = _matching_events(
        events,
        type_filter=type_filter,
        reason_filter=reason_filter,
        keyword_filter=keyword_filter,
    )
    effective_tail = DEFAULT_EVENT_TAIL if tail is None else tail
    if effective_tail > 0:
        visible = matching[-effective_tail:]
    else:
        visible = matching
    return BoundedCollection(
        items=visible,
        shown=len(visible),
        total=len(matching),
        truncated=len(visible) < len(matching),
    )


def filter_events(
    events: list[dict],
    *,
    type_filter: Optional[str] = None,
    reason_filter: Optional[str] = None,
    keyword_filter: Optional[str] = None,
    tail: Optional[int] = None,
) -> list[dict]:
    """Apply optional filters and return a bounded event window."""
    return _event_window(
        events,
        type_filter=type_filter,
        reason_filter=reason_filter,
        keyword_filter=keyword_filter,
        tail=tail,
    ).items


def public_event(event: dict) -> dict[str, Any]:
    """Project a platform event onto the compact public diagnostic schema."""
    message = event.get("message") or event.get("content")
    timestamp = (
        event.get("last_timestamp")
        or event.get("first_timestamp")
        or event.get("timestamp")
        or event.get("age")
    )
    projected = {
        "time": _fmt_timestamp(timestamp) if timestamp not in (None, "") else None,
        # Who the row is about, when that is not simply "the workload this
        # command was pointed at". `instance` is attached by the command layer
        # for per-pod queries (the raw `object_id` is a handle and stays out of
        # output); `node` comes from node events, where the platform names the
        # node itself; `job` is attached when one stream merges several jobs,
        # which is the only case where the workload itself is ambiguous. A
        # single-workload row carries none of them, so all three keys are
        # absent.
        "node": event.get("node_name") or event.get("node"),
        "instance": event.get("instance"),
        "job": event.get("job"),
        "type": event_type(event),
        "reason": event.get("reason"),
        "message": message,
        "count": event.get("count"),
    }
    return {
        key: (
            json_formatter.sanitize_text(value, redact_paths=True)
            if isinstance(value, str)
            else value
        )
        for key, value in projected.items()
        if value not in (None, "")
    }


# The optional subject column: who a row is about, when that is narrower than
# the workload -- or, for `job`, when one stream carries several workloads. At
# most one of these is present in any single stream.
# The width is a cap the column shrinks below, so a generous `job` only costs
# table width when the names really are long -- and job names here routinely
# are. At 28 two runs of one experiment truncate to the same prefix, which
# leaves the column carrying none of the information it exists to add.
_SUBJECT_COLUMNS = (
    ("node", "Node", 24),
    ("instance", "Instance", 28),
    ("job", "Job", 48),
)

# Kubernetes-shaped classification, which only the workload event streams
# carry. Notebook lifecycle events are `{time, message}` and nothing else, so
# for them these would be three columns of dashes.
_CLASSIFICATION_COLUMNS = (
    ("type", "Type", 10, "left"),
    ("reason", "Reason", 32, "left"),
    ("count", "Count", 7, "right"),
)


def render_events_table(events: list[dict]) -> None:
    """Print compact event diagnostics to stdout.

    A column appears only when the rows carry it — a merged per-pod or
    multi-node window is unreadable without its subject, while a notebook's
    lifecycle window would only gain columns of dashes.
    """
    if not events:
        click.echo("(no events)")
        return

    items = [public_event(event) for event in events]
    subjects = [
        (key, title, width)
        for key, title, width in _SUBJECT_COLUMNS
        if any(item.get(key) for item in items)
    ]
    classification = [
        (key, title, width, align)
        for key, title, width, align in _CLASSIFICATION_COLUMNS
        if any(item.get(key) for item in items)
    ]

    def row(item: dict[str, Any]) -> tuple[str, ...]:
        cells = [str(item.get("time") or "-")]
        cells.extend(str(item.get(key) or "-") for key, _title, _width in subjects)
        cells.extend(
            str(item.get(key) or "-") for key, _title, _width, _align in classification
        )
        cells.append(str(item.get("message") or "-").replace("\n", " "))
        return tuple(cells)

    rows = [row(item) for item in items]
    header = (
        "Time",
        *(title for _key, title, _width in subjects),
        *(title for _key, title, _width, _align in classification),
        "Message",
    )
    max_widths = (
        19,
        *(width for _key, _title, width in subjects),
        *(width for _key, _title, width, _align in classification),
        80,
    )
    aligns = [
        "left",
        *("left" for _ in subjects),
        *(align for _key, _title, _width, align in classification),
        "left",
    ]
    widths = [
        column_width(title, [row[index] for row in rows], max_width=max_width)
        for index, (title, max_width) in enumerate(zip(header, max_widths))
    ]
    click.echo(
        "\n".join(
            render_table(
                header,
                rows,
                widths,
                aligns=aligns,
                line_char="─",
            )
        )
    )


def emit_events(
    ctx: Context,
    events: list[dict],
    *,
    total: int | None = None,
) -> None:
    """Render events for stdout according to JSON vs human preference."""
    page = BoundedCollection(
        items=events,
        shown=len(events),
        total=max(len(events), total or 0),
        truncated=total is not None and total > len(events),
    )
    if ctx.json_output:
        public_events = [public_event(event) for event in events]
        click.echo(
            json_formatter.format_json(
                {
                    "items": public_events,
                    **page.metadata(),
                }
            )
        )
    else:
        render_events_table(events)
        notice = truncation_notice(
            page,
            full_option=f"--tail {page.total}",
        )
        if notice:
            click.echo(notice)


def _event_key(event: dict) -> tuple[str, ...]:
    return tuple(
        str(event.get(key) or "")
        for key in (
            "object_id",
            "object_type",
            "node_name",
            "reason",
            "message",
            "from",
            "event_type",
            "first_timestamp",
            "last_timestamp",
            "count",
        )
    )


class _RecentEventKeys:
    """Bound duplicate suppression for long-running event followers."""

    def __init__(self, limit: int = FOLLOW_EVENT_KEY_LIMIT) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        self._limit = limit
        self._keys: set[tuple[str, ...]] = set()
        self._order: deque[tuple[str, ...]] = deque()

    def remember(self, event: dict) -> bool:
        """Return true when the event was not present in the recent window."""
        key = _event_key(event)
        if key in self._keys:
            return False
        self._keys.add(key)
        self._order.append(key)
        if len(self._order) > self._limit:
            self._keys.discard(self._order.popleft())
        return True

    def __len__(self) -> int:
        return len(self._keys)


def _fetch_filtered_events(
    *,
    fetch: Callable[[], list[dict]],
    type_filter: Optional[str],
    reason_filter: Optional[str],
    keyword_filter: Optional[str] = None,
    tail: Optional[int] = None,
) -> BoundedCollection[dict]:
    events = fetch()
    return _event_window(
        events,
        type_filter=type_filter,
        reason_filter=reason_filter,
        keyword_filter=keyword_filter,
        tail=tail,
    )


def run_events_command(
    ctx: Context,
    *,
    fetch: Callable[[], list[dict]],
    type_filter: Optional[str],
    reason_filter: Optional[str],
    keyword_filter: Optional[str] = None,
    tail: Optional[int] = None,
    follow: bool = False,
    interval: int = 5,
) -> None:
    """Shared entrypoint used by `inspire job events` / `inspire hpc events`.

    `fetch` is the per-job-kind platform call returning a list[dict].
    """
    if follow and ctx.json_output:
        raise click.UsageError(
            "--json --follow is not supported for events. Drop --json to follow, "
            "or drop --follow for a one-shot JSON fetch."
        )

    try:
        page = _fetch_filtered_events(
            fetch=fetch,
            type_filter=type_filter,
            reason_filter=reason_filter,
            keyword_filter=keyword_filter,
            tail=tail,
        )
    except Exception as e:
        exit_with_error(
            ctx,
            "APIError",
            f"Could not fetch events: {scrub_raw_ids(e)}",
            EXIT_API_ERROR,
        )
        return
    if follow:
        seen = _RecentEventKeys()
        for event in page.items:
            seen.remember(event)
        render_events_table(page.items)
        while True:
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                click.echo()
                return
            try:
                current = _fetch_filtered_events(
                    fetch=fetch,
                    type_filter=type_filter,
                    reason_filter=reason_filter,
                    keyword_filter=keyword_filter,
                    tail=tail,
                )
            except Exception as e:
                exit_with_error(
                    ctx,
                    "APIError",
                    f"Could not fetch events: {scrub_raw_ids(e)}",
                    EXIT_API_ERROR,
                )
                return
            fresh = []
            for event in current.items:
                if seen.remember(event):
                    fresh.append(event)
            if not fresh:
                continue
            render_events_table(fresh)

    emit_events(
        ctx=ctx,
        events=page.items,
        total=page.total,
    )
