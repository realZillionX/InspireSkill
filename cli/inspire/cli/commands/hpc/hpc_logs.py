"""`inspire hpc logs <name>` — aggregated platform logs for an HPC job.

Same shape and the same output budget as `inspire job logs`: a bounded latest
snapshot by default, `--all` for the complete one-shot log, and truncation
metadata an Agent can act on. The differences are all forced by the platform:

* `hpc.GetJobLog` wants the **namespaced instance names**, which are platform
  handles. They never reach output — every line and every JSON record is
  labelled with the Role / Rank identity that `inspire hpc instances` prints,
  and `--instance` selects on that same identity.
* The Action refuses any sorter, ignores `PageNumber`, and truncates a large
  answer by dropping its newest records — so ordering, the "last N" window, and
  the month-wide window cap are all enforced on this side.
"""

from __future__ import annotations

from inspire.services.hpc.hpc_logs import (
    log_time_range as _log_time_range,
    labelled_logs as _labelled_logs,
)

from typing import Any, Optional

import click

from inspire.cli.commands.hpc.hpc_commands import (
    HPCInstanceSelectionError,
    _fetch_hpc_instances,
    _reject_hpc_name_at_boundary,
    _run_readonly_hpc_operation,
    hpc_instance_views,
    select_hpc_instance_views,
)
from inspire.cli.commands.job.job_logs import (
    DEFAULT_LOG_CHARACTER_LIMIT,
    DEFAULT_PLATFORM_LOG_RECORDS,
    LOG_TEXT_KEYS,
    _emit_truncation_hint,
    _format_web_log_line,
    _select_web_logs,
    _window_to_minutes,
)
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_LOG_NOT_FOUND,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.services.utils import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import NAME_PICK_HELP
from inspire.config import Config, ConfigError
from inspire.platform.web.browser_api.hpc_jobs import (
    list_hpc_job_logs,
)
from inspire.platform.web.session import SessionExpiredError, get_web_session

_INSTANCE_SCAN_LIMIT = 500
_NAME_RESOLUTION_LIMIT = 10000
_WINDOW_PADDING_MS = 10 * 60 * 1000
_FALLBACK_WINDOW_MS = 24 * 60 * 60 * 1000


def _format_hpc_logs(logs: list[dict[str, Any]]) -> str:
    if not logs:
        return "No HPC logs found."
    return "\n".join(["HPC Logs", *(_format_web_log_line(item) for item in logs)])


@click.command("logs")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option(
    "--instance",
    "instance_selectors",
    multiple=True,
    metavar="ROLE",
    help=(
        "Read only this instance, named by the Role (and Rank, when a role has "
        "replicas) column of `inspire hpc instances` — for example slurmd or "
        "slurmctld. Repeat for several. Default: every instance in the job."
    ),
)
@click.option(
    "--window",
    default=None,
    help=(
        "Relative time window, e.g. 30m or 2h. Default: the run's own lifetime. "
        "The platform refuses windows wider than a month, so longer ones are "
        "shortened to the most recent 30 days."
    ),
)
@click.option(
    "--tail",
    type=click.IntRange(1),
    help=(
        f"Show the last N records. Default one-shot output uses "
        f"{DEFAULT_PLATFORM_LOG_RECORDS}."
    ),
)
@click.option("--head", type=click.IntRange(1), help="Show the first N records.")
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help=(
        f"Maximum records fetched per request (default: "
        f"{DEFAULT_PLATFORM_LOG_RECORDS})."
    ),
)
@click.option(
    "--all",
    "all_output",
    is_flag=True,
    help=(
        f"Show the complete one-shot log without the record or the default "
        f"{DEFAULT_LOG_CHARACTER_LIMIT}-character limit. "
        "Cannot be combined with --tail, --head, or --limit."
    ),
)
@pass_context
def hpc_logs(
    ctx: Context,
    name: str,
    workspace: str,
    pick: Optional[int],
    instance_selectors: tuple[str, ...],
    window: Optional[str],
    tail: Optional[int],
    head: Optional[int],
    limit: Optional[int],
    all_output: bool,
) -> None:
    """Read aggregated platform logs for an HPC job.

    One-shot output shows a bounded latest snapshot and applies a total
    character budget; use ``--all`` only when the complete log is required.
    Records are ordered oldest-first here, because the platform refuses to
    sort them.

    \b
    Examples:
      inspire hpc logs prep-a --workspace CPU资源空间
      inspire hpc logs prep-a --workspace CPU资源空间 --tail 50
      inspire hpc logs prep-a --workspace CPU资源空间 --instance slurmd
      inspire hpc logs prep-a --workspace CPU资源空间 --window 2h
      inspire --json hpc logs prep-a --workspace CPU资源空间
      inspire hpc logs prep-a --workspace CPU资源空间 --all
    """
    name = _reject_hpc_name_at_boundary(ctx, name)

    if tail is not None and head is not None:
        _handle_error(
            ctx,
            "InvalidUsage",
            "--tail and --head cannot be used together.",
            EXIT_VALIDATION_ERROR,
        )
        return

    all_conflicts = [
        option
        for option, enabled in (
            ("--tail", tail is not None),
            ("--head", head is not None),
            ("--limit", limit is not None),
        )
        if enabled
    ]
    if all_output and all_conflicts:
        _handle_error(
            ctx,
            "InvalidUsage",
            f"--all cannot be combined with {', '.join(all_conflicts)}.",
            EXIT_VALIDATION_ERROR,
        )
        return

    try:
        since_minutes = _window_to_minutes(window) if window else None
    except click.BadParameter as exc:
        _handle_error(ctx, "ValidationError", str(exc), EXIT_VALIDATION_ERROR)
        return

    record_limit = limit or DEFAULT_PLATFORM_LOG_RECORDS
    fetch_size = max(record_limit, tail or 0, head or 0)

    # `page_size` truncates from the *end*: asking for 5 of 134 records returns
    # the oldest 5, and `PageNumber` is ignored, so there is no way to ask the
    # platform for the newest N. Every selection that reads from the end has to
    # pull the whole window first and take its tail here. `--head` is the one
    # that does not — the platform's own truncation is already what it wants.
    wants_newest = head is None

    def _load(job_id: str, live_session) -> tuple:  # noqa: ANN001
        instances, _total = _fetch_hpc_instances(
            job_id,
            limit=_INSTANCE_SCAN_LIMIT,
            session=live_session,
            show_all=True,
        )
        views = select_hpc_instance_views(
            hpc_instance_views(instances),
            instance_selectors,
        )
        if not views:
            return views, [], 0, False
        start_ms, end_ms, clamped = _log_time_range(instances, since_minutes)
        pod_names = [view.handle for view in views]

        def _fetch(page_size: int) -> tuple[list[dict[str, Any]], int]:
            return list_hpc_job_logs(
                pod_names=pod_names,
                start_timestamp_ms=start_ms,
                end_timestamp_ms=end_ms,
                page_size=page_size,
                job_id=job_id,
                session=live_session,
            )

        logs, total = _fetch(DEFAULT_PLATFORM_LOG_RECORDS if all_output else fetch_size)
        if (all_output or wants_newest) and total > len(logs):
            # The transport holds `page_size` at the gateway ceiling, so a very
            # long log still comes back short — the truncation metadata reports
            # the real `total` either way.
            logs, total = _fetch(total)
        return views, logs, total, clamped

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        views, logs, total, clamped = _run_readonly_hpc_operation(
            ctx,
            session=session,
            name=name,
            workspace=workspace,
            limit=_NAME_RESOLUTION_LIMIT,
            pick=pick,
            operation=_load,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except HPCInstanceSelectionError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
        return
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    if not views:
        _handle_error(
            ctx,
            "LogNotFound",
            f"No instances found for HPC job {name}",
            EXIT_LOG_NOT_FOUND,
            hint=(
                "A job that never scheduled has no pods to read from. Check "
                f"`inspire hpc events {name} --workspace {workspace}`."
            ),
        )
        return

    selection = _select_web_logs(
        _labelled_logs(logs, views),
        total=total,
        tail=tail,
        head=head,
        record_limit=record_limit,
        all_output=all_output,
    )

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "logs": selection.logs,
                    "truncated": selection.truncated,
                    "shown": selection.shown,
                    "total": selection.total,
                    "limit": selection.limit,
                    "character_limit": selection.character_limit,
                    "shown_chars": selection.shown_chars,
                },
                preserve_paths=LOG_TEXT_KEYS,
            )
        )
        return

    if clamped:
        click.echo(
            "Window shortened to the most recent 30 days; the platform rejects "
            "log queries wider than a month.",
            err=True,
        )
    click.echo(_format_hpc_logs(selection.logs))
    if selection.truncated:
        _emit_truncation_hint(
            shown=selection.shown,
            total=selection.total,
            unit="records",
            all_output=all_output,
        )


__all__ = ["hpc_logs"]
