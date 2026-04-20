"""Shared helpers for `inspire job events` / `inspire hpc events`.

Event payloads from Browser API (`/train_job/job_event_list` and
`/hpc_jobs/events/list`) share most fields — `reason`, `message`, `from`,
`first_timestamp`, `last_timestamp`, `age`, `object_id`, `object_type` —
but not all. Train jobs carry a K8s-native `type` (`Normal` / `Warning`),
HPC jobs don't. Both sets are lossy after GC (returning `[]` for
long-completed jobs is the steady state).

Design mirrors `job logs`: fetch live → cache to
`~/.inspire/events/<id>.events.json` → format for stdout. Re-running with
`--from-cache` skips the network call; the command is otherwise idempotent
and safe to re-run.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import click

from inspire.cli.formatters import json_formatter


_EVENTS_CACHE_DIR = Path.home() / ".inspire" / "events"


def events_cache_path(job_id: str) -> Path:
    """Local cache path for events of a given job id."""
    return _EVENTS_CACHE_DIR / f"{job_id}.events.json"


def write_events_cache(job_id: str, events: list[dict]) -> Path:
    """Write events to the local cache, best-effort; returns the path written.

    Silently no-ops on I/O error (cache is a convenience, not load-bearing).
    """
    path = events_cache_path(job_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "job_id": job_id,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "events": events,
        }
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except OSError:
        pass
    return path


def read_events_cache(job_id: str) -> Optional[list[dict]]:
    """Load cached events if present; returns None when no cache exists."""
    path = events_cache_path(job_id)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        events = data.get("events")
        return events if isinstance(events, list) else None
    except (OSError, json.JSONDecodeError):
        return None


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
    if tail and tail > 0:
        out = out[-tail:]
    return out


def render_events_table(events: list[dict]) -> None:
    """Print events as a dense table to stdout.

    Columns: TIME (last_timestamp) · TYPE (Normal/Warning/–) · REASON · FROM · MESSAGE.
    Missing `type` (HPC events lack it) renders as blank.
    """
    if not events:
        click.echo("(no events — platform GCs events for long-completed jobs)")
        return

    def row(e: dict) -> tuple[str, str, str, str, str]:
        return (
            _fmt_timestamp(e.get("last_timestamp")),
            str(e.get("type", "") or "-"),
            str(e.get("reason", "") or "-"),
            str(e.get("from", "") or "-"),
            str(e.get("message", "") or "").replace("\n", " "),
        )

    rows = [row(e) for e in events]
    header = ("TIME", "TYPE", "REASON", "FROM", "MESSAGE")
    widths = [
        max(len(header[i]), max((len(r[i]) for r in rows), default=0))
        for i in range(4)
    ]
    widths[2] = min(widths[2], 40)
    widths[3] = min(widths[3], 30)

    click.echo(
        f"{header[0].ljust(widths[0])}  "
        f"{header[1].ljust(widths[1])}  "
        f"{header[2].ljust(widths[2])}  "
        f"{header[3].ljust(widths[3])}  "
        f"{header[4]}"
    )
    click.echo("-" * (sum(widths) + 8 + 40))
    for r in rows:
        reason = r[2] if len(r[2]) <= widths[2] else r[2][: widths[2] - 1] + "…"
        src = r[3] if len(r[3]) <= widths[3] else r[3][: widths[3] - 1] + "…"
        line = (
            f"{r[0].ljust(widths[0])}  "
            f"{r[1].ljust(widths[1])}  "
            f"{reason.ljust(widths[2])}  "
            f"{src.ljust(widths[3])}  "
            f"{r[4]}"
        )
        if r[1].lower() == "warning":
            click.echo(click.style(line, fg="yellow"))
        else:
            click.echo(line)


def emit_events(
    ctx_json: bool,
    local_json: bool,
    job_id: str,
    events: list[dict],
    cache_path: Path,
) -> None:
    """Render events for stdout according to JSON vs human preference."""
    if ctx_json or local_json:
        click.echo(
            json_formatter.format_json(
                {
                    "job_id": job_id,
                    "count": len(events),
                    "cache_path": str(cache_path),
                    "events": events,
                }
            )
        )
    else:
        render_events_table(events)
        click.echo()
        click.echo(
            click.style(f"cached → {cache_path}", fg="white", dim=True),
            err=True,
        )


def run_events_command(
    ctx,
    *,
    job_id: str,
    fetch: Callable[[], list[dict]],
    json_output_local: bool,
    from_cache: bool,
    type_filter: Optional[str],
    reason_filter: Optional[str],
    tail: Optional[int],
) -> None:
    """Shared entrypoint used by `inspire job events` / `inspire hpc events`.

    `fetch` is the per-job-kind Browser API call returning a list[dict].
    """
    if from_cache:
        cached = read_events_cache(job_id)
        if cached is None:
            click.secho(
                f"No cached events for {job_id}; run without --from-cache to fetch.",
                fg="yellow",
                err=True,
            )
            cached = []
        events = cached
    else:
        try:
            events = fetch()
        except Exception as e:  # defensive: helpers already swallow, but belt-and-suspenders
            click.secho(f"events fetch failed: {e}", fg="red", err=True)
            events = []
        write_events_cache(job_id, events)

    filtered = filter_events(
        events,
        type_filter=type_filter,
        reason_filter=reason_filter,
        tail=tail,
    )

    emit_events(
        ctx_json=bool(getattr(ctx, "json_output", False)),
        local_json=json_output_local,
        job_id=job_id,
        events=filtered,
        cache_path=events_cache_path(job_id),
    )
