"""`inspire tensorboard` — the platform's TensorBoard objects, end to end.

A TensorBoard here is a small always-CPU workload (fixed 1 CPU / 2 GiB) that
reads event files off the shared disk and serves them over HTTP. The lifecycle
half of this module creates, starts, stops and deletes those objects; the
reading half lives in :mod:`tensorboard_data`, which queries the running app
so an Agent gets scalar series instead of a web address it cannot open.
"""

from __future__ import annotations
from inspire.services.tensorboard.tensorboard_status import normalize_status

import logging
from typing import Any, Optional
import click
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter
from inspire.services.utils import json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.services.utils.collections import (
    DEFAULT_COLLECTION_LIMIT,
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import (
    exit_with_error as _handle_error,
    require_confirmation,
)
from inspire.cli.utils.id_resolver import (
    NAME_PICK_HELP,
    forget_resource_identity,
    reject_id_at_boundary,
    remember_resource_identity,
    resolve_by_name,
)
from inspire.cli.utils.job_submit import select_project_for_workspace
from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session
from inspire.services.tensorboard.tensorboards import current_user_id as current_user_id
from inspire.services.tensorboard.tensorboards import fetch_boards as fetch_boards
from inspire.services.tensorboard.tensorboards import resolve_group_id as _resolve_group_id
from inspire.services.tensorboard.tensorboards import find_created_board as _find_created_board
from inspire.services.tensorboard.tensorboards import await_status as _await_status





logger = logging.getLogger(__name__)

RESOURCE_TYPE = "tensorboard"
LIST_COMMAND = "inspire tensorboard list"

# One board is 1 CPU / 2 GiB, so a workspace holds a lot of them; the resolver
# needs the whole set to be sure a name is unambiguous.
_NAME_SCAN_LIMIT = 2000

DEFAULT_AUTO_STOP_HOURS = 24.0
MAX_AUTO_STOP_HOURS = browser_api_module.MAX_AUTO_STOP_MS / 3_600_000

_STATUS_CHOICES = ("running", "stopped", "creating")

# A create that answers success has not necessarily produced a row yet, and
# `CreateTensorboard` returns no id at all, so the new board has to be found by
# name. These bound that search, not the board's startup.
_CREATE_LOOKUP_ATTEMPTS = 10
_CREATE_LOOKUP_INTERVAL_SECONDS = 2.0

_STOP_CONFIRM_ATTEMPTS = 20
_STOP_CONFIRM_INTERVAL_SECONDS = 3.0




def workspace_id_for(session, workspace: str) -> str:  # noqa: ANN001
    workspace_id = select_workspace_id(
        explicit_workspace_name=workspace,
        session=session,
    )
    if workspace_id is None:
        raise ConfigError("--workspace is required.")
    return workspace_id


def reject_tensorboard_id(ctx: Context, name: str) -> str:
    return reject_id_at_boundary(
        ctx,
        name,
        resource_type=RESOURCE_TYPE,
        list_command=LIST_COMMAND,
    )




def resolve_board(
    ctx: Context,
    *,
    session,  # noqa: ANN001
    name: str,
    workspace_id: str,
    pick: Optional[int] = None,
    require_live: bool = False,
) -> str:
    """Resolve a board name to its `tb-…` handle."""

    def _lister() -> list[dict[str, Any]]:
        return [
            {
                "name": board.name,
                "id": board.tb_id,
                "status": board.status,
                "created_at": board.created_at,
            }
            # `keyword` is a server-side substring match on name, which is a
            # strict superset of the exact match the resolver then applies.
            for board in fetch_boards(
                session,
                workspace_id=workspace_id,
                limit=_NAME_SCAN_LIMIT,
                keyword=name,
            )
        ]

    return resolve_by_name(
        ctx,
        name=name,
        resource_type=RESOURCE_TYPE,
        list_candidates=_lister,
        pick_index=pick,
        session=session,
        workspace_id=workspace_id,
        owner_scope="self",
        require_live=require_live,
        list_command=LIST_COMMAND,
    )


def board_row(board: Any) -> dict[str, str]:
    return {
        "name": scrub_raw_ids(board.name),
        "status": normalize_status(board.status),
        "job": scrub_raw_ids(board.job_name),
        "project": scrub_raw_ids(board.project_name),
        "summary_path": scrub_raw_ids(board.summary_path),
    }


def board_detail(board: Any) -> dict[str, Any]:
    """The public view of one board. `url` is deliberately absent.

    An Agent has no browser, and the address is not the answer to any question
    it can act on — `inspire tensorboard tags` and `scalars` read that app on
    its behalf. What stays here is where the data comes from and whether the
    board is up.
    """
    auto_stop_hours = ""
    if board.auto_stop_ms.isdigit():
        auto_stop_hours = f"{int(board.auto_stop_ms) / 3_600_000:g}"
    return {
        "name": scrub_raw_ids(board.name),
        "status": normalize_status(board.status),
        "job": scrub_raw_ids(board.job_name),
        "project": scrub_raw_ids(board.project_name),
        "compute_group": scrub_raw_ids(board.compute_group_name),
        "summary_path": scrub_raw_ids(board.summary_path),
        "auto_stop_hours": auto_stop_hours,
        "created_at": human_formatter.format_epoch(board.created_at),
    }


def format_board_detail(detail: dict[str, Any]) -> str:
    labels = [
        ("Name", "name"),
        ("Status", "status"),
        ("Job", "job"),
        ("Project", "project"),
        ("Compute Group", "compute_group"),
        ("Summary Path", "summary_path"),
        ("Auto Stop", "auto_stop_hours"),
        ("Created", "created_at"),
    ]
    width = max(len(label) for label, _ in labels)
    lines = []
    for label, key in labels:
        value = str(detail.get(key) or "")
        if not value:
            continue
        if key == "auto_stop_hours":
            value = f"{value}h after start"
        lines.append(f"{label.ljust(width)}  {value}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@click.command("list")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--status",
    type=click.Choice(_STATUS_CHOICES),
    default=None,
    help="Show only boards in this state.",
)
@click.option(
    "--job",
    "job_name",
    default="",
    metavar="NAME",
    help="Show only boards attached to this training job.",
)
@click.option(
    "--limit",
    "-n",
    "limit",
    type=click.IntRange(1),
    default=None,
    help=f"Maximum rows to print (default: {DEFAULT_COLLECTION_LIMIT}).",
)
@click.option("--all", "show_all", is_flag=True, help="Print every TensorBoard.")
@pass_context
def list_tensorboards_cmd(
    ctx: Context,
    workspace: str,
    status: Optional[str],
    job_name: str,
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List this account's TensorBoards in one workspace.

    `Summary Path` is the shared-disk directory the event files are read from,
    and `Job` is empty for a board that was created on its own rather than
    against a training run.

    \b
    Examples:
        inspire tensorboard list --workspace 分布式训练空间
        inspire tensorboard list --workspace 分布式训练空间 --status running
        inspire tensorboard list --workspace 分布式训练空间 --job glm-sft-run3
    """
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    request_limit = (
        effective_limit if effective_limit is not None else DEFAULT_COLLECTION_LIMIT
    )
    # A job filter is applied client-side, so the page has to be wide enough to
    # contain the matches before it is narrowed.
    if job_name:
        request_limit = max(request_limit, _NAME_SCAN_LIMIT)

    try:
        Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = workspace_id_for(session, workspace)
        boards = fetch_boards(
            session,
            workspace_id=workspace_id,
            limit=request_limit,
            status=status or "",
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
        return
    except Exception as e:  # noqa: BLE001
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)
        return

    if job_name:
        boards = [board for board in boards if board.job_name == job_name]

    page = bound_collection(
        [board_row(board) for board in boards],
        limit=effective_limit,
        total=len(boards),
    )
    if ctx.json_output:
        click.echo(json_formatter.format_json({"items": page.items, **page.metadata()}))
        return

    if not page.items:
        click.echo("No TensorBoards found.")
        return

    headers = ["Name", "Status", "Job", "Project", "Summary Path"]
    keys = ["name", "status", "job", "project", "summary_path"]
    widths = [
        column_width(header, [row[key] for row in page.items])
        for header, key in zip(headers, keys)
    ]
    values = [[row[key] for key in keys] for row in page.items]
    click.echo("\n".join(render_table(headers, values, widths, line_char="─")))
    notice = truncation_notice(page)
    if notice:
        click.echo(notice)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@click.command("status")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
@pass_context
def status_tensorboard(
    ctx: Context,
    name: str,
    workspace: str,
    pick: Optional[int],
) -> None:
    """Show one TensorBoard's placement, summary path and auto-stop window.

    Only a `running` board answers `inspire tensorboard tags` and `scalars`;
    start it first if this reports `stopped`.
    """
    name = reject_tensorboard_id(ctx, name)
    try:
        Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = workspace_id_for(session, workspace)
        tb_id = resolve_board(
            ctx, session=session, name=name, workspace_id=workspace_id, pick=pick
        )
        board = browser_api_module.get_tensorboard(tb_id, session=session)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
        return
    except Exception as e:  # noqa: BLE001
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)
        return

    detail = board_detail(board)
    if ctx.json_output:
        click.echo(json_formatter.format_json(detail))
        return
    click.echo(format_board_detail(detail))


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------






@click.command("create")
@click.option("--name", "-n", required=True, metavar="NAME", help="TensorBoard name.")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option("--project", required=True, metavar="NAME", help="Project name.")
@click.option(
    "--group",
    required=True,
    metavar="NAME",
    help="Compute group name; it must run the tensorboard job type.",
)
@click.option(
    "--summary-path",
    required=True,
    metavar="PATH",
    help="Shared-disk directory holding the event files.",
)
@click.option(
    "--job",
    "job_name",
    default="",
    metavar="NAME",
    help="Attach the board to this training job; omit for a standalone board.",
)
@click.option(
    "--auto-stop-hours",
    type=click.FloatRange(0, MAX_AUTO_STOP_HOURS, min_open=True),
    default=DEFAULT_AUTO_STOP_HOURS,
    show_default=True,
    help=f"Stop the board this long after start (platform maximum: {MAX_AUTO_STOP_HOURS:g}h).",
)
@pass_context
def create_tensorboard_cmd(
    ctx: Context,
    name: str,
    workspace: str,
    project: str,
    group: str,
    summary_path: str,
    job_name: str,
    auto_stop_hours: float,
) -> None:
    """Start a TensorBoard on one summary directory.

    The instance is fixed at 1 CPU / 2 GiB, so there is no quota to pick — a
    compute group is the only placement input, and it has to advertise the
    tensorboard job type. The board reads whatever event files already exist
    under `--summary-path`; nothing has to be re-written for it.

    `--job` only records which training run the board belongs to, so it shows
    up on that job's row in the console. It does not derive the summary path:
    pass the directory your training code writes events to either way.

    The board stops itself after `--auto-stop-hours` (platform maximum 72).
    Read it with `inspire tensorboard tags` and `inspire tensorboard scalars`.

    \b
    Examples:
        inspire tensorboard create -n glm-sft --workspace 分布式训练空间 --project 前沿课题探索 --group 训练区-H200-1号机房 --summary-path /inspire/hdd/project/<project>/<user>/runs/glm-sft
        inspire tensorboard create -n glm-sft --workspace 分布式训练空间 --project 前沿课题探索 --group 训练区-H200-1号机房 --summary-path /inspire/hdd/project/<project>/<user>/runs/glm-sft --job glm-sft-run3 --auto-stop-hours 6
    """
    name = reject_tensorboard_id(ctx, name)
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = workspace_id_for(session, workspace)
        group_id = _resolve_group_id(session, workspace_id=workspace_id, group=group)
        project_info, _ = select_project_for_workspace(
            config,
            workspace_id=workspace_id,
            requested=project,
        )

        job_id = ""
        if job_name:
            job_id = _resolve_job_id(
                ctx, session=session, name=job_name, workspace_id=workspace_id
            )

        browser_api_module.create_tensorboard(
            name=name,
            workspace_id=workspace_id,
            project_id=project_info.project_id,
            logic_compute_group_id=group_id,
            summary_path=summary_path,
            auto_stop_ms=int(auto_stop_hours * 3_600_000),
            job_id=job_id,
            session=session,
        )
        board = _find_created_board(session, workspace_id=workspace_id, name=name)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except ValueError as e:
        _handle_error(ctx, "ValidationError", scrub_raw_ids(e), EXIT_VALIDATION_ERROR)
        return
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
        return
    except Exception as e:  # noqa: BLE001
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)
        return

    if board is None:
        _handle_error(
            ctx,
            "APIError",
            f"The platform accepted the TensorBoard {scrub_raw_ids(name)!r} but no "
            "board with that name appeared.",
            EXIT_API_ERROR,
            hint=f"Check with `{LIST_COMMAND} --workspace {scrub_raw_ids(workspace)}`.",
        )
        return

    remember_resource_identity(
        session=session,
        resource_type=RESOURCE_TYPE,
        resource_id=board.tb_id,
        name=board.name,
        workspace_id=workspace_id,
        owner_scope="self",
        status=board.status,
        created_at=board.created_at,
    )

    detail = board_detail(board)
    if ctx.json_output:
        click.echo(json_formatter.format_json({**detail, "created": True}))
        return
    click.echo(human_formatter.format_mutation_success("TensorBoard", "created", name))
    click.echo(format_board_detail(detail))


def _resolve_job_id(
    ctx: Context,
    *,
    session,  # noqa: ANN001
    name: str,
    workspace_id: str,
) -> str:
    from inspire.services.tensorboard.tensorboards import job_candidates
    def _lister():
        return job_candidates(session=session, name=name, workspace_id=workspace_id)

    return resolve_by_name(
        ctx,
        name=name,
        resource_type="job",
        list_candidates=_lister,
        session=session,
        workspace_id=workspace_id,
        owner_scope="self",
        list_command="inspire job list",
    )


# ---------------------------------------------------------------------------
# start / stop / delete
# ---------------------------------------------------------------------------




@click.command("start")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
@pass_context
def start_tensorboard_cmd(
    ctx: Context,
    name: str,
    workspace: str,
    pick: Optional[int],
) -> None:
    """Restart a stopped TensorBoard.

    The board keeps its summary path and auto-stop window, so nothing has to
    be re-specified; the auto-stop clock restarts from this point.
    """
    name = reject_tensorboard_id(ctx, name)
    try:
        Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = workspace_id_for(session, workspace)
        tb_id = resolve_board(
            ctx,
            session=session,
            name=name,
            workspace_id=workspace_id,
            pick=pick,
            require_live=True,
        )
        browser_api_module.start_tensorboard(tb_id, session=session)
        status = _await_status(session, tb_id, leaving="stopped")
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
        return
    except Exception as e:  # noqa: BLE001
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)
        return

    if status == "stopped":
        _handle_error(
            ctx,
            "APIError",
            f"TensorBoard {scrub_raw_ids(name)!r} is still stopped; the platform "
            "accepted the start request without acting on it.",
            EXIT_API_ERROR,
        )
        return

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {"name": name, "status": "started", "board_status": status}
            )
        )
        return
    click.echo(human_formatter.format_mutation_success("TensorBoard", "started", name))


@click.command("stop")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
@pass_context
def stop_tensorboard_cmd(
    ctx: Context,
    name: str,
    workspace: str,
    pick: Optional[int],
) -> None:
    """Stop a running TensorBoard and release its CPU.

    The record survives with its summary path, so `inspire tensorboard start`
    brings the same board back. Stopping an already-stopped board succeeds.
    """
    name = reject_tensorboard_id(ctx, name)
    try:
        Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = workspace_id_for(session, workspace)
        tb_id = resolve_board(
            ctx,
            session=session,
            name=name,
            workspace_id=workspace_id,
            pick=pick,
            require_live=True,
        )
        browser_api_module.stop_tensorboard(tb_id, session=session)
        status = _await_status(session, tb_id, reaching="stopped")
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
        return
    except Exception as e:  # noqa: BLE001
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)
        return

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {"name": name, "status": "stopped", "board_status": status}
            )
        )
        return
    click.echo(human_formatter.format_mutation_success("TensorBoard", "stopped", name))


@click.command("delete")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
@pass_context
def delete_tensorboard_cmd(
    ctx: Context,
    name: str,
    workspace: str,
    yes: bool,
    pick: Optional[int],
) -> None:
    """Delete a stopped TensorBoard record.

    The event files on the shared disk are untouched — only the board that was
    serving them goes away, and a new board on the same `--summary-path` reads
    exactly the same data. A running board is refused; `stop` it first.
    """
    name = reject_tensorboard_id(ctx, name)
    require_confirmation(
        ctx,
        yes=yes,
        prompt=(
            f"Permanently delete TensorBoard '{scrub_raw_ids(name)}'? "
            "This cannot be undone."
        ),
        message="TensorBoard deletion requires confirmation.",
    )

    try:
        Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = workspace_id_for(session, workspace)
        tb_id = resolve_board(
            ctx,
            session=session,
            name=name,
            workspace_id=workspace_id,
            pick=pick,
            require_live=True,
        )
        browser_api_module.delete_tensorboard(tb_id, session=session)
        forget_resource_identity(
            session=session,
            resource_type=RESOURCE_TYPE,
            resource_id=tb_id,
            name=name,
            workspace_id=workspace_id,
            owner_scope="self",
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
        return
    except Exception as e:  # noqa: BLE001
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)
        return

    if ctx.json_output:
        click.echo(json_formatter.format_json({"name": name, "status": "deleted"}))
        return
    click.echo(human_formatter.format_mutation_success("TensorBoard", "deleted", name))


__all__ = [
    "create_tensorboard_cmd",
    "delete_tensorboard_cmd",
    "list_tensorboards_cmd",
    "start_tensorboard_cmd",
    "status_tensorboard",
    "stop_tensorboard_cmd",
]
