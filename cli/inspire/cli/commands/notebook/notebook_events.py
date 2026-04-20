"""`inspire notebook events <id>` — lifecycle timeline for a notebook instance.

Payload comes from Browser API `POST /api/v1/notebook/events` via
`browser_api.notebooks.list_notebook_events`. Output is cached to
`~/.inspire/events/<notebook_id>.events.json` on every successful fetch.

**Shape differs from train / HPC events**: the platform returns a
platform-level **lifecycle timeline** (free-form `content` string + epoch-ms
`created_at`), not raw K8s events. So typical messages look like
"Successfully assigned … to qb-prod-…", "Pulling image …", "Started
container …", "Notebook stopped from user timedShutdown", etc. There is no
K8s-native `type` (Normal/Warning) or structured `reason`. The wrapper
synthesizes `message` ← `content` and `last_timestamp` ← `created_at` so the
shared renderer in `cli.utils.events` can print them the same way as
`inspire job events`; `--type` / `--reason` filters are accepted for
symmetry but will rarely match (both fields are blank).

The platform GC's events for long-terminated notebooks, so an empty list is
a normal steady-state response for old DELETED / STOPPED instances — not an
error.

Notebooks run as a single pod; there is no per-instance events endpoint and
thus no `--instance` flag. If you need deeper pod-level diagnostics, fall
back to `inspire notebook status <id>`.
"""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import Context, pass_context
from inspire.cli.utils.events import run_events_command
from inspire.platform.web.browser_api.notebooks import list_notebook_events


@click.command("events")
@click.argument("notebook_id")
@click.option(
    "--json",
    "json_output_local",
    is_flag=True,
    help="Output as JSON. Equivalent to top-level `--json`.",
)
@click.option(
    "--from-cache",
    is_flag=True,
    help="Read from `~/.inspire/events/<id>.events.json` and skip the live fetch.",
)
@click.option(
    "--type",
    "type_filter",
    help="Filter events by `type` (Normal / Warning; case-insensitive prefix match).",
)
@click.option(
    "--reason",
    "reason_filter",
    help="Filter events whose `reason` contains this substring (case-insensitive).",
)
@click.option(
    "--tail",
    type=int,
    help="Show only the last N events (applied after --type/--reason).",
)
@pass_context
def events(
    ctx: Context,
    notebook_id: str,
    json_output_local: bool,
    from_cache: bool,
    type_filter: Optional[str],
    reason_filter: Optional[str],
    tail: Optional[int],
) -> None:
    """Show K8s events for a notebook instance (scheduling, image pulls, pod lifecycle).

    \b
    Examples:
      inspire notebook events <id>
      inspire --json notebook events <id>
      inspire notebook events <id> --type Warning
      inspire notebook events <id> --reason FailedScheduling
      inspire notebook events <id> --from-cache
    """
    run_events_command(
        ctx,
        job_id=notebook_id,
        fetch=lambda: list_notebook_events(notebook_id),
        json_output_local=json_output_local,
        from_cache=from_cache,
        type_filter=type_filter,
        reason_filter=reason_filter,
        tail=tail,
    )
