"""`inspire ray logs <name>` — aggregated platform logs for a Ray cluster.

Same shape and the same output budget as `inspire job logs` and
`inspire hpc logs`: a bounded latest snapshot by default, `--all` for the
complete one-shot log, and truncation metadata an Agent can act on. What is
specific to Ray:

* `ray.GetJobLog` is scoped by pod name, not by job id — see
  :func:`inspire.platform.web.browser_api.ray_jobs.list_ray_job_logs`. Those
  names are platform handles, so they never reach output: every line and every
  JSON record is labelled with the Role / Type identity that
  `inspire ray instances` prints, and `--instance` selects on that same
  identity.
* Filtering by role is worth more here than in the fixed-shape workloads. A
  Ray cluster mixes one driver head with elastic worker groups, so
  `--instance head` and `--instance <group>` separate the driver's own output
  from the fan-out that usually dominates the record count.
"""

from __future__ import annotations

from typing import Any, Optional

import click

from inspire.cli.commands.job.job_logs import (
    DEFAULT_LOG_CHARACTER_LIMIT,
    DEFAULT_PLATFORM_LOG_RECORDS,
    LOG_TEXT_KEYS,
    _emit_truncation_hint,
    _format_web_log_line,
    _select_web_logs,
    _web_log_time_range,
    _window_to_minutes,
)
from inspire.cli.commands.ray.ray_commands import (
    RayInstanceSelectionError,
    RayInstanceView,
    _fetch_ray_instances,
    _reject_ray_name_at_boundary,
    _run_readonly_ray_operation,
    ray_instance_views,
    select_ray_instance_views,
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
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import NAME_PICK_HELP
from inspire.config import Config, ConfigError
from inspire.platform.web.browser_api.ray_jobs import (
    RAY_LOG_MAX_WINDOW_MS,
    get_ray_job_detail,
    list_ray_job_logs,
)
from inspire.platform.web.session import SessionExpiredError, get_web_session

_INSTANCE_SCAN_LIMIT = 500
_NAME_RESOLUTION_LIMIT = 10000


def _clamped_window(
    detail: dict[str, Any],
    since_minutes: int | None,
) -> tuple[int, int, bool]:
    """Pick the query window, then hold it inside the platform's month cap."""
    start_ms, end_ms = _web_log_time_range(detail, since_minutes)
    clamped = end_ms - start_ms > RAY_LOG_MAX_WINDOW_MS
    if clamped:
        start_ms = end_ms - RAY_LOG_MAX_WINDOW_MS
    return start_ms, end_ms, clamped


def _labelled_logs(
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


def _format_ray_logs(logs: list[dict[str, Any]]) -> str:
    if not logs:
        return "No Ray logs found."
    return "\n".join(["Ray Logs", *(_format_web_log_line(item) for item in logs)])


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
        "Read only this instance, named by the Role / Type (and Rank, when "
        "several share one) column of `inspire ray instances` — for example "
        "head, worker, or a worker-group name. Repeat for several. "
        "Default: every pod in the cluster."
    ),
)
@click.option(
    "--window",
    default=None,
    help=(
        "Relative time window, e.g. 30m or 2h. Default: the cluster's own "
        "lifetime. Windows wider than a month are shortened to the most recent "
        "30 days."
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
def logs_ray(
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
    """Read aggregated platform logs for a Ray (弹性计算) job.

    One-shot output shows a bounded latest snapshot and applies a total
    character budget; use ``--all`` only when the complete log is required.
    Records are ordered oldest-first here, because the platform is not asked
    to sort them.

    \b
    Examples:
      inspire ray logs pipeline --workspace CPU资源空间
      inspire ray logs pipeline --workspace CPU资源空间 --tail 50
      inspire ray logs pipeline --workspace CPU资源空间 --instance head
      inspire ray logs pipeline --workspace CPU资源空间 --instance decode
      inspire ray logs pipeline --workspace CPU资源空间 --window 30m
      inspire --json ray logs pipeline --workspace CPU资源空间
      inspire ray logs pipeline --workspace CPU资源空间 --all
    """
    name = _reject_ray_name_at_boundary(ctx, name)

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

    def _load(ray_job_id: str, live_session) -> tuple:  # noqa: ANN001
        instances, _total = _fetch_ray_instances(
            ray_job_id,
            limit=_INSTANCE_SCAN_LIMIT,
            session=live_session,
            show_all=True,
        )
        views = select_ray_instance_views(
            ray_instance_views(instances),
            instance_selectors,
        )
        if not views:
            return views, [], 0, False

        detail = get_ray_job_detail(ray_job_id, session=live_session)
        start_ms, end_ms, clamped = _clamped_window(detail, since_minutes)
        pod_names = [view.handle for view in views]
        logs, total = list_ray_job_logs(
            pod_names=pod_names,
            start_timestamp_ms=start_ms,
            end_timestamp_ms=end_ms,
            page_size=DEFAULT_PLATFORM_LOG_RECORDS if all_output else fetch_size,
            session=live_session,
        )
        if all_output and total > len(logs):
            logs, total = list_ray_job_logs(
                pod_names=pod_names,
                start_timestamp_ms=start_ms,
                end_timestamp_ms=end_ms,
                page_size=total,
                session=live_session,
            )
        return views, logs, total, clamped

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        views, logs, total, clamped = _run_readonly_ray_operation(
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
    except RayInstanceSelectionError as e:
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
            f"No instances found for Ray job {name}",
            EXIT_LOG_NOT_FOUND,
            hint=(
                "A cluster that never scheduled has no pods to read from. Check "
                f"`inspire ray events {name} --workspace {workspace}`."
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
            "Window shortened to the most recent 30 days; log queries wider "
            "than a month are refused.",
            err=True,
        )
    click.echo(_format_ray_logs(selection.logs))
    if selection.truncated:
        _emit_truncation_hint(
            shown=selection.shown,
            total=selection.total,
            unit="records",
            all_output=all_output,
        )


__all__ = ["logs_ray"]
