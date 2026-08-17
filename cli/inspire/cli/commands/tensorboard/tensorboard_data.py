"""`inspire tensorboard tags` / `scalars` — read the running board, not its URL.

The platform serves each board as a real TensorBoard app behind the same
session cookie, so its data plane answers JSON. That is what makes a
TensorBoard useful to an Agent: the training curves stop being a page someone
has to look at and become numbers a command can return.
"""

from __future__ import annotations

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
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import NAME_PICK_HELP
from inspire.cli.utils.metrics_shared import render_sparkline
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session

from .tensorboard_commands import (
    reject_tensorboard_id,
    resolve_board,
    workspace_id_for,
)

# A scalar series is unbounded — a long run logs tens of thousands of points —
# and the whole series is never the answer to "how is training going".
DEFAULT_POINT_BUDGET = 20


def _live_board(
    ctx: Context,
    *,
    name: str,
    workspace: str,
    pick: Optional[int],
):  # noqa: ANN202
    """Resolve *name* to a board that is actually up, or exit explaining why not."""
    Config.from_files_and_env(require_credentials=False)
    session = get_web_session()
    workspace_id = workspace_id_for(session, workspace)
    tb_id = resolve_board(
        ctx, session=session, name=name, workspace_id=workspace_id, pick=pick
    )
    board = browser_api_module.get_tensorboard(tb_id, session=session)
    if board.status != "running":
        _handle_error(
            ctx,
            "ValidationError",
            f"TensorBoard {scrub_raw_ids(name)!r} is {scrub_raw_ids(board.status)}; "
            "only a running board serves data.",
            EXIT_VALIDATION_ERROR,
            hint=(
                f"Start it with `inspire tensorboard start {scrub_raw_ids(name)} "
                f"--workspace {scrub_raw_ids(workspace)}`."
            ),
        )
    return session, board


def _summarize(points: list[tuple[float, int, float]]) -> dict[str, Any]:
    """Reduce one series to the shape a training-health question actually asks."""
    by_step = sorted(points, key=lambda point: point[1])
    values = [value for _, _, value in by_step]
    first_step, first_value = by_step[0][1], by_step[0][2]
    last_step, last_value = by_step[-1][1], by_step[-1][2]
    return {
        "count": len(by_step),
        "first_step": first_step,
        "first_value": first_value,
        "last_step": last_step,
        "last_value": last_value,
        "min": min(values),
        "max": max(values),
    }


def _tail(points: list[tuple[float, int, float]], budget: int) -> list[list[float]]:
    by_step = sorted(points, key=lambda point: point[1])
    return [[step, value] for _, step, value in by_step[-budget:]]


def _collect_series(
    session,  # noqa: ANN001
    board,  # noqa: ANN001
    *,
    run: str,
    tag: str,
) -> list[dict[str, Any]]:
    tags_by_run = browser_api_module.read_tensorboard_scalar_tags(
        board.url, session=session
    )
    collected: list[dict[str, Any]] = []
    for run_name, tags in sorted(tags_by_run.items()):
        if run and run_name != run:
            continue
        for tag_name in tags:
            if tag and tag_name != tag:
                continue
            points = browser_api_module.read_tensorboard_scalar_series(
                board.url, run=run_name, tag=tag_name, session=session
            )
            if not points:
                continue
            collected.append(
                {
                    "run": run_name,
                    "tag": tag_name,
                    "points": points,
                    **_summarize(points),
                }
            )
    return collected


@click.command("tags")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
@pass_context
def tensorboard_tags(
    ctx: Context,
    name: str,
    workspace: str,
    pick: Optional[int],
) -> None:
    """List the runs and scalar tags a running TensorBoard can serve.

    A board pointed straight at a directory of event files reports the single run `.`; one pointed at a parent directory reports a run per subdirectory. Both the run and the tag are what `inspire tensorboard scalars` selects on.

    No tags on a running board means the summary path holds no scalar events — a wrong path and a run that has not logged yet look identical from here.
    """
    name = reject_tensorboard_id(ctx, name)
    try:
        session, board = _live_board(ctx, name=name, workspace=workspace, pick=pick)
        runs = browser_api_module.read_tensorboard_runs(board.url, session=session)
        tags_by_run = browser_api_module.read_tensorboard_scalar_tags(
            board.url, session=session
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

    payload = {
        "name": scrub_raw_ids(name),
        "summary_path": scrub_raw_ids(board.summary_path),
        "runs": [scrub_raw_ids(run) for run in runs],
        "scalar_tags": {
            scrub_raw_ids(run): [scrub_raw_ids(tag) for tag in tags]
            for run, tags in sorted(tags_by_run.items())
        },
    }
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
        return

    if not runs:
        click.echo(
            f"No runs under {scrub_raw_ids(board.summary_path)}. "
            "The board is up but found no event files."
        )
        return

    for run in runs:
        tags = tags_by_run.get(run) or []
        click.echo(f"run {scrub_raw_ids(run)}")
        if not tags:
            click.echo("  (no scalar tags)")
            continue
        for tag in tags:
            click.echo(f"  {scrub_raw_ids(tag)}")


@click.command("scalars")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--tag",
    default="",
    metavar="TAG",
    help="Read only this scalar tag; omit to summarize every tag.",
)
@click.option(
    "--run",
    default="",
    metavar="RUN",
    help="Read only this run; omit to cover every run.",
)
@click.option(
    "--points",
    type=click.IntRange(0),
    default=0,
    metavar="N",
    help=(
        "Also print the last N (step, value) points per series "
        f"(default: summary only; {DEFAULT_POINT_BUDGET} is a reasonable N)."
    ),
)
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
@pass_context
def tensorboard_scalars(
    ctx: Context,
    name: str,
    workspace: str,
    tag: str,
    run: str,
    points: int,
    pick: Optional[int],
) -> None:
    """Read scalar series from a running TensorBoard.

    Each series is reported as first/last value against its step range, plus min and max over the whole run — enough to answer whether a loss is still falling, whether an eval metric has plateaued, or whether a run diverged. `--points N` adds the last N raw (step, value) pairs when the trend line is not enough.

    Points are ordered by step, not by the order the event files list them: a resumed run or a multi-worker writer interleaves them, and "the last point" is a question about steps.

    \b
    Examples:
        inspire tensorboard scalars glm-sft --workspace 分布式训练空间
        inspire tensorboard scalars glm-sft --workspace 分布式训练空间 --tag train/loss --points 20
    """
    name = reject_tensorboard_id(ctx, name)
    try:
        session, board = _live_board(ctx, name=name, workspace=workspace, pick=pick)
        series = _collect_series(session, board, run=run, tag=tag)
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
                {
                    "name": scrub_raw_ids(name),
                    "summary_path": scrub_raw_ids(board.summary_path),
                    "series": [
                        {
                            "run": scrub_raw_ids(item["run"]),
                            "tag": scrub_raw_ids(item["tag"]),
                            "count": item["count"],
                            "first_step": item["first_step"],
                            "first_value": item["first_value"],
                            "last_step": item["last_step"],
                            "last_value": item["last_value"],
                            "min": item["min"],
                            "max": item["max"],
                            **(
                                {"points": _tail(item["points"], points)}
                                if points
                                else {}
                            ),
                        }
                        for item in series
                    ],
                }
            )
        )
        return

    if not series:
        selector = " / ".join(part for part in (run, tag) if part)
        click.echo(
            f"No scalar data{f' for {scrub_raw_ids(selector)}' if selector else ''} "
            f"under {scrub_raw_ids(board.summary_path)}."
        )
        return

    headers = ["Run", "Tag", "Points", "Steps", "First", "Last", "Min", "Max", "Trend"]
    rows = [
        [
            scrub_raw_ids(item["run"]),
            scrub_raw_ids(item["tag"]),
            str(item["count"]),
            f"{item['first_step']}-{item['last_step']}",
            f"{item['first_value']:.4g}",
            f"{item['last_value']:.4g}",
            f"{item['min']:.4g}",
            f"{item['max']:.4g}",
            render_sparkline(
                [value for _, _, value in sorted(item["points"], key=lambda p: p[1])],
                width=16,
            ),
        ]
        for item in series
    ]
    widths = [
        column_width(header, [row[index] for row in rows])
        for index, header in enumerate(headers)
    ]
    click.echo("\n".join(render_table(headers, rows, widths, line_char="─")))

    if not points:
        return
    for item in series:
        click.echo(
            f"\n{scrub_raw_ids(item['run'])} / {scrub_raw_ids(item['tag'])} "
            f"(last {min(points, item['count'])} of {item['count']})"
        )
        for step, value in _tail(item["points"], points):
            click.echo(f"  {int(step):>10}  {value:.6g}")


__all__ = ["tensorboard_scalars", "tensorboard_tags"]
