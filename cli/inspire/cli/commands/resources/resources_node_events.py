"""`inspire resources node-events <node>` — events belonging to a node itself.

Every other event command in this CLI answers for a workload: why *this job*
was not scheduled, why *this pod* restarted. This one is keyed by the node —
kernel OOM kills, cordons, `NodeNotSchedulable`, controller removals — which
is the other half of "why did my run die on that machine". Get the node name
from the `Node` column of `<workload> instances` or `<workload> status`.
"""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.events import (
    DEFAULT_EVENT_TAIL,
    event_sort_key,
    run_events_command,
)
from inspire.config import Config, ConfigError
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import (
    AuthenticationError,
    SessionExpiredError,
    get_web_session,
)

_NODE_EVENT_PAGE_SIZE = 200
_NODE_EVENT_MAX_PAGES = 5  # newest 1,000 rows; see command help


@click.command("node-events")
@click.argument("nodes", metavar="NODE...", nargs=-1, required=True)
@click.option(
    "--from",
    "from_filter",
    metavar="COMPONENT",
    help=(
        "Only events reported by this component, e.g. kubelet, "
        "kernel-monitor, node-controller."
    ),
)
@click.option(
    "--type",
    "type_filter",
    type=click.Choice(["Normal", "Warning"], case_sensitive=False),
    help="Filter by K8s event type.",
)
@click.option(
    "--reason",
    "reason_filter",
    metavar="REASON",
    help="Filter events whose `reason` contains this substring (case-insensitive).",
)
@click.option(
    "--tail",
    type=click.IntRange(1),
    default=DEFAULT_EVENT_TAIL,
    show_default=True,
    help="Maximum recent events to display.",
)
@click.option(
    "--follow",
    "-f",
    is_flag=True,
    help=(
        "Follow the timeline and print new events. Runs until interrupted; it never exits on its own, "
        "not even once the node reaches a terminal state."
    ),
)
@click.option(
    "--interval",
    type=click.IntRange(1),
    default=5,
    show_default=True,
    help="Polling interval in seconds for --follow.",
)
@pass_context
def node_events(
    ctx: Context,
    nodes: tuple[str, ...],
    from_filter: Optional[str],
    type_filter: Optional[str],
    reason_filter: Optional[str],
    tail: int,
    follow: bool,
    interval: int,
) -> None:
    """Show platform events for one or more cluster nodes.

    Read node names off the `Node` column of `inspire job instances`, `inspire
    hpc instances`, or `inspire notebook status`. Several nodes answer in one
    merged timeline with a `Node` column. A node the cluster does not know is
    not an error — it simply has no events, so check the spelling before
    reading silence as a healthy node. The command scans the newest 1,000
    events; `--from` is applied by the platform before that window, while
    `--type` and `--reason` filter the scanned rows locally.

    \b
    Examples:
      inspire resources node-events qb-prod-4090-gpu040
      inspire resources node-events qb-prod-4090-gpu040 hpc-compute531 --tail 50
      inspire resources node-events qb-prod-4090-gpu040 --type Warning
      inspire resources node-events qb-prod-4090-gpu040 --from kernel-monitor
      inspire --json resources node-events qb-prod-4090-gpu040
    """
    if follow and ctx.json_output:
        raise click.UsageError(
            "--json --follow is not supported for events. Drop --json to follow, "
            "or drop --follow for a one-shot JSON fetch."
        )

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except (AuthenticationError, SessionExpiredError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
        return

    component = (from_filter or "").strip().lower()

    def _fetch() -> list[dict]:
        try:
            events = browser_api_module.list_node_events(
                list(nodes),
                page_size=_NODE_EVENT_PAGE_SIZE,
                max_pages=_NODE_EVENT_MAX_PAGES,
                sort_ascending=False,
                from_component=component or None,
                session=session,
            )
        except (AuthenticationError, SessionExpiredError) as e:
            _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
            return []
        except Exception as e:  # noqa: BLE001
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
            return []
        # The API is read newest-first so a bounded scan cannot discard the
        # recent tail; renderers still need oldest-first chronology.
        return sorted(events, key=event_sort_key)

    run_events_command(
        ctx,
        fetch=_fetch,
        type_filter=type_filter,
        reason_filter=reason_filter,
        tail=tail,
        follow=follow,
        interval=interval,
    )
