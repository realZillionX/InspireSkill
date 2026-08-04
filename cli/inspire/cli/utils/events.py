"""Shared helpers for `inspire job events` / `inspire hpc events`.

Platform event records share most fields — `reason`, `message`, `from`,
`first_timestamp`, `last_timestamp`, `age`, `object_id`, `object_type` —
but not all. Train jobs carry a Kubernetes-style `type` (`Normal` /
`Warning`), HPC jobs don't. Both sets are lossy after GC (returning `[]`
for long-completed jobs is the steady state).

Events are always fetched from the live platform API. Local caches are not a
source of truth for user-visible diagnostics.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import click

from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.raw_ids import scrub_raw_ids


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


def filter_events(
    events: list[dict],
    *,
    type_filter: Optional[str] = None,
    reason_filter: Optional[str] = None,
    keyword_filter: Optional[str] = None,
    tail: Optional[int] = None,
) -> list[dict]:
    """Apply optional filters + tail to an events list."""
    out = events
    if type_filter:
        needle = type_filter.lower()
        out = [e for e in out if str(e.get("type", "")).lower() == needle]
    if reason_filter:
        needle = reason_filter.lower()
        out = [e for e in out if needle in str(e.get("reason", "")).lower()]
    if keyword_filter:
        needle = keyword_filter.lower()
        out = [
            e
            for e in out
            if any(
                needle in str(e.get(key, "")).lower()
                for key in ("reason", "message", "from", "type", "content")
            )
        ]
    if tail and tail > 0:
        out = out[-tail:]
    return out


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
        "type": event.get("type"),
        "reason": event.get("reason"),
        "message": message,
        "count": event.get("count"),
    }
    return {
        key: scrub_raw_ids(value) if isinstance(value, str) else value
        for key, value in projected.items()
        if value not in (None, "")
    }


def render_events_table(events: list[dict]) -> None:
    """Print compact event diagnostics to stdout."""
    if not events:
        click.echo("(no events — platform GCs events for long-completed jobs)")
        return

    def row(event: dict) -> tuple[str, str, str, str, str]:
        item = public_event(event)
        return (
            str(item.get("time") or "-"),
            str(item.get("type") or "-"),
            str(item.get("reason") or "-"),
            str(item.get("count") or "-"),
            str(item.get("message") or "-").replace("\n", " "),
        )

    rows = [row(e) for e in events]
    header = ("Time", "Type", "Reason", "Count", "Message")
    widths = [
        column_width(header[0], [row[0] for row in rows], max_width=19),
        column_width(header[1], [row[1] for row in rows], max_width=10),
        column_width(header[2], [row[2] for row in rows], max_width=32),
        column_width(header[3], [row[3] for row in rows], max_width=7),
        column_width(header[4], [row[4] for row in rows], max_width=80),
    ]
    click.echo(
        "\n".join(
            render_table(
                header,
                rows,
                widths,
                aligns=["left", "left", "left", "right", "left"],
                line_char="─",
            )
        )
    )


def emit_events(
    ctx_json: bool,
    local_json: bool,
    resource_type: str,
    resource_name: str,
    events: list[dict],
) -> None:
    """Render events for stdout according to JSON vs human preference."""
    if ctx_json or local_json:
        public_events = [public_event(event) for event in events]
        click.echo(
            json_formatter.format_json(
                {
                    "resource": resource_type,
                    "name": resource_name,
                    "count": len(public_events),
                    "events": public_events,
                }
            )
        )
    else:
        render_events_table(events)


def _event_key(event: dict) -> tuple[str, ...]:
    return tuple(
        str(event.get(key) or "")
        for key in (
            "object_id",
            "object_type",
            "reason",
            "message",
            "from",
            "first_timestamp",
            "last_timestamp",
            "count",
        )
    )


def _fetch_filtered_events(
    *,
    fetch: Callable[[], list[dict]],
    type_filter: Optional[str],
    reason_filter: Optional[str],
    keyword_filter: Optional[str] = None,
) -> list[dict]:
    try:
        events = fetch()
    except Exception as e:  # defensive: helpers already swallow, but belt-and-suspenders
        click.secho(f"events fetch failed: {scrub_raw_ids(e)}", fg="red", err=True)
        events = []
    return filter_events(
        events,
        type_filter=type_filter,
        reason_filter=reason_filter,
        keyword_filter=keyword_filter,
        tail=None,
    )


def run_events_command(
    ctx,
    *,
    resource_id: str,
    resource_type: str,
    resource_name: str,
    fetch: Callable[[], list[dict]],
    json_output_local: bool,
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
    del resource_id
    json_mode = bool(getattr(ctx, "json_output", False)) or json_output_local
    if follow and json_mode:
        raise click.UsageError(
            "--json --follow is not supported for events. Drop --json to follow, "
            "or drop --follow for a one-shot JSON fetch."
        )

    filtered = _fetch_filtered_events(
        fetch=fetch,
        type_filter=type_filter,
        reason_filter=reason_filter,
        keyword_filter=keyword_filter,
    )
    initial = filtered[-tail:] if tail and tail > 0 else filtered

    if follow:
        seen = {_event_key(event) for event in filtered}
        render_events_table(initial)
        while True:
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                click.echo()
                return
            current = _fetch_filtered_events(
                fetch=fetch,
                type_filter=type_filter,
                reason_filter=reason_filter,
                keyword_filter=keyword_filter,
            )
            fresh = []
            for event in current:
                key = _event_key(event)
                if key not in seen:
                    fresh.append(event)
                seen.add(key)
            if not fresh:
                continue
            render_events_table(fresh)
        return

    emit_events(
        ctx_json=bool(getattr(ctx, "json_output", False)),
        local_json=json_output_local,
        resource_type=resource_type,
        resource_name=resource_name,
        events=initial,
    )
