"""`inspire serving logs` — aggregated platform logs for one deployment.

`inference_serving.GetServingLog` is pod-scoped, not serving-scoped: the
request body carries only `filter.podNames` plus the time window, so this
command resolves the name, reads the deployment's instances, and queries
those pod names. A serving with no instances therefore has no logs to read,
which is reported instead of being sent to the platform as an empty filter.

Record, line, and character budgets are the ones `inspire job logs` already
applies. They are imported rather than re-derived so the two log commands
cannot drift apart; the only serving-specific piece is the default time
window, because `GetServingLog` has no serving handle to derive one from.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import click

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
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import NAME_PICK_HELP, reject_id_at_boundary
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session

from .serving_instances import (
    ServingInstanceSelectionError,
    select_serving_instance_views,
    serving_instance_views,
)
from .serving_commands import (
    _resolve_workspace_id,
    _run_readonly_serving_operation,
)

# A serving is a long-running service, so there is no finish time to bound the
# window with the way `job logs` does. One day covers the useful case (what is
# this deployment doing now) without asking the log store for a wide scan.
DEFAULT_SERVING_LOG_WINDOW_MINUTES = 24 * 60

# Pods are listed once per call; a deployment never has enough replicas for
# this to page.
_INSTANCE_FETCH_SIZE = 200


def _reject_serving_instance_name(ctx: Context, value: str) -> str:
    """Enforce the Name-only boundary for the `--instance` selector."""
    return reject_id_at_boundary(
        ctx,
        value,
        resource_type="serving instance",
        list_command="inspire serving instances <serving-name> --workspace <workspace>",
    )


def _format_serving_logs(logs: list[dict[str, Any]]) -> str:
    if not logs:
        return "No serving logs found."
    return "\n".join(_format_web_log_line(item) for item in logs)


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
    "instance_names",
    multiple=True,
    metavar="RANK",
    help=(
        "Read only this replica, named by the Name column of `inspire serving "
        "instances` — `rank=0`, or just `0`. Repeat for several. "
        "Default: every replica of the deployment."
    ),
)
@click.option(
    "--window",
    default=None,
    help="Relative time window, e.g. 30m, 2h, or 1d. Default: 24h.",
)
@click.option("--tail", type=click.IntRange(1), help="Show the last N records.")
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
        f"Show every record in the window without the record or the default "
        f"{DEFAULT_LOG_CHARACTER_LIMIT}-character limit. "
        "Cannot be combined with --tail, --head, or --limit."
    ),
)
@pass_context
def logs_serving(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    pick: Optional[int],
    instance_names: tuple[str, ...],
    window: Optional[str],
    tail: Optional[int],
    head: Optional[int],
    limit: Optional[int],
    all_output: bool,
) -> None:
    """Read aggregated platform logs for an inference serving.

    \b
    Logs are collected per replica pod and merged in timestamp order. Output
    shows a bounded latest snapshot under a total character budget; use
    ``--all`` only when the complete window is required. Replicas that were
    replaced by a scale or rollback keep their logs until the platform's log
    store ages them out.

    \b
    Examples:
        inspire serving logs my-serving --workspace 分布式训练空间
        inspire serving logs my-serving --workspace 分布式训练空间 --window 30m
        inspire serving logs my-serving --workspace 分布式训练空间 --tail 20
        inspire serving logs my-serving --workspace 分布式训练空间 --all
    """
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

    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="serving",
        list_command="inspire serving list --workspace <workspace>",
    )
    if instance_names:
        instance_names = tuple(
            _reject_serving_instance_name(ctx, value) for value in instance_names
        )

    try:
        since_minutes = (
            _window_to_minutes(window)
            if window
            else DEFAULT_SERVING_LOG_WINDOW_MINUTES
        )
    except click.BadParameter as exc:
        _handle_error(ctx, "ValidationError", str(exc), EXIT_VALIDATION_ERROR)
        return

    record_limit = limit or DEFAULT_PLATFORM_LOG_RECORDS
    fetch_size = max(record_limit, tail or 0, head or 0)

    def _load(serving_id: str, live_session):  # noqa: ANN001
        instances, _total = browser_api_module.list_serving_instances(
            serving_id,
            page=1,
            page_size=_INSTANCE_FETCH_SIZE,
            session=live_session,
        )
        # The selector speaks the Name column of `inspire serving instances`
        # (`rank=0`); the pod handle it maps to is namespaced, which is what
        # `GetServingLog` requires and what no output ever shows.
        views = select_serving_instance_views(
            serving_instance_views(instances),
            instance_names,
        )
        pod_names = [view.handle for view in views]
        if not pod_names:
            return [], 0, pod_names

        end_ms = int(time.time() * 1000)
        start_ms = max(0, end_ms - since_minutes * 60 * 1000)
        initial_size = DEFAULT_PLATFORM_LOG_RECORDS if all_output else fetch_size
        logs, total = browser_api_module.list_serving_logs(
            pod_names=pod_names,
            start_timestamp_ms=start_ms,
            end_timestamp_ms=end_ms,
            page_size=initial_size,
            inference_serving_id=serving_id,
            session=live_session,
        )
        # `total` counts the whole window, so `--all` needs a second pass once
        # the first response says how much is actually there.
        if all_output and total > len(logs):
            logs, total = browser_api_module.list_serving_logs(
                pod_names=pod_names,
                start_timestamp_ms=start_ms,
                end_timestamp_ms=end_ms,
                page_size=total,
                inference_serving_id=serving_id,
                session=live_session,
            )
        return logs, total, pod_names

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(workspace)
        logs, total, pod_names = _run_readonly_serving_operation(
            ctx,
            name=name,
            workspace_id=workspace_id,
            session=session,
            pick=pick,
            operation=_load,
        )

        if not pod_names:
            _handle_error(
                ctx,
                "LogNotFound",
                f"No instances found for serving {scrub_raw_ids(name)}.",
                EXIT_LOG_NOT_FOUND,
                hint=(
                    "A stopped deployment has no replicas to read logs from. "
                    "Check `inspire serving status <name> --workspace <workspace>`."
                ),
            )
            return

        selection = _select_web_logs(
            logs,
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
                        "name": scrub_raw_ids(name),
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

        click.echo(_format_serving_logs(selection.logs))
        if selection.truncated:
            _emit_truncation_hint(
                shown=selection.shown,
                total=selection.total,
                unit="records",
                all_output=all_output,
            )

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except ServingInstanceSelectionError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = ["logs_serving"]
