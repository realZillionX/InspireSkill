"""Notebook subcommands."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import click

from .notebook_create_flow import maybe_run_post_start, run_notebook_create
from .notebook_lookup import (
    _current_user_lookup_failure_message,
    _list_notebooks_for_workspaces,
    _resolve_notebook_id,
    _run_notebook_operation_with_stale_handle_retry,
    _sort_notebook_items,
    _try_get_current_user_ids,
)
from .notebook_presenters import _print_notebook_detail, _print_notebook_list
from .public_output import public_notebook, public_operation
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.collection_output import (
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
)
from inspire.cli.utils.notebook_cli import (
    WEB_AUTH_HINT,
    get_base_url,
    load_config,
    require_web_session,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.task_priority import task_priority_option
from inspire.cli.utils.notebook_post_start import (
    NO_WAIT_POST_START_WARNING,
    resolve_notebook_post_start_spec,
)
from inspire.config import ConfigError
from inspire.config.workspaces import (
    resolve_workspace_operation_scope,
    resolve_workspace_query_scope,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.platform.web.browser_api import NotebookFailedError

logger = logging.getLogger(__name__)


def _print_notebook_wait_progress(notebook: dict, status: str, events: str) -> None:
    display_status = status or "UNKNOWN"
    sub_status = str(notebook.get("sub_status") or "").strip()
    suffix = f" ({sub_status})" if sub_status else ""
    click.echo(f"Status: {scrub_raw_ids(display_status)}{scrub_raw_ids(suffix)}")

    latest_event = next(
        (line.strip() for line in reversed(events.splitlines()) if line.strip()),
        "",
    )
    if latest_event:
        click.echo(f"Latest event: {scrub_raw_ids(latest_event)}")


def _workspace_display(session, workspace_id: str) -> str:  # noqa: ANN001
    names = getattr(session, "all_workspace_names", None)
    if isinstance(names, dict):
        name = names.get(workspace_id)
        if name:
            return str(name)
    return "(workspace name unavailable)"


def _has_workspace_display_name(item: dict) -> bool:
    workspace = item.get("workspace")
    if isinstance(workspace, dict):
        if workspace.get("name") or workspace.get("workspace_name") or workspace.get("workspaceName"):
            return True
    return bool(item.get("workspace_name") or item.get("workspaceName"))


def _with_workspace_display_name(item: dict, workspace_name: str) -> dict:
    result = dict(item)
    if workspace_name and not _has_workspace_display_name(result):
        result["workspace_name"] = workspace_name
    return result


@click.command("create")
@click.option(
    "--name",
    "-n",
    required=True,
    metavar="NAME",
    help="Notebook name",
)
@click.option(
    "--workspace",
    metavar="NAME",
    help="Workspace name. Required unless supplied by --profile.",
)
@click.option(
    "--project",
    "-p",
    metavar="NAME",
    help="Project name. Required unless supplied by --profile.",
)
@click.option(
    "--group",
    "group",
    metavar="NAME",
    help=(
        "Full compute group name copied from the same quota row as --quota. "
        "Required unless supplied by --profile. "
        "Partial matches are not accepted."
    ),
)
@click.option(
    "--quota",
    "-q",
    default=None,
    metavar="SPEC",
    help=(
        "Resource quota as 'gpu,cpu,mem' (mem in GiB). "
        "Example: '1,20,200' for 1 GPU + 20 CPU + 200 GiB. "
        "Use '0,4,32' for CPU-only. "
        "The triple must match a quota row in the workspace (see 'inspire notebook quota'); "
        "pass --group <full compute group name> to disambiguate. "
        "Required unless supplied by --profile."
    ),
)
@click.option(
    "--image",
    "-i",
    metavar="NAME|URL",
    help="Image name or URL. Required unless supplied by --profile.",
)
@click.option(
    "--profile",
    "profile_name",
    default=None,
    metavar="NAME",
    help="Notebook condition profile providing workspace/project/group/quota/image.",
)
@click.option(
    "--shm-size",
    type=click.IntRange(1),
    default=None,
    help="Shared memory size in GiB (default: INSPIRE_SHM_SIZE/job.shm_size, else 32)",
)
@click.option(
    "--auto-stop/--no-auto-stop",
    default=False,
    help=(
        "Request idle auto-stop. This does not disable manager auto-recycle "
        "rules or workspace lifetime caps."
    ),
)
@click.option(
    "--wait/--no-wait",
    default=True,
    help=(
        "Wait for notebook to reach RUNNING status "
        "(default: enabled; still required when a post-start action is configured)"
    ),
)
@click.option(
    "--post-start",
    type=str,
    default=None,
    help="Post-start action after RUNNING: none or a shell command",
)
@click.option(
    "--post-start-script",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    default=None,
    help="Local shell script to upload and run in the notebook after RUNNING",
)
@task_priority_option()
@click.option(
    "--node",
    "node",
    default=None,
    metavar="NAME",
    help=(
        "Pin the notebook to a specific cluster node by name (e.g. qb-prod-gpu1736). "
        "The node must belong to the selected compute group; the platform rejects "
        "a mismatch. Omit to let the scheduler place it."
    ),
)
@pass_context
def create_notebook_cmd(
    ctx: Context,
    name: Optional[str],
    workspace: Optional[str],
    quota: Optional[str],
    project: Optional[str],
    image: Optional[str],
    shm_size: Optional[int],
    auto_stop: bool,
    wait: bool,
    post_start: Optional[str],
    post_start_script: Optional[Path],
    priority: Optional[int],
    group: Optional[str],
    node: Optional[str],
    profile_name: Optional[str],
) -> None:
    """Create a new interactive notebook instance.

    \b
    Examples:
        inspire notebook create --workspace 分布式训练空间 --project CI-情境智能 \
          --image sandbox-base:latest --group H200-2号机房 -q 1,20,200
        inspire notebook create --workspace CPU资源空间 --project CI-情境智能 \
          --image sandbox-base:latest --group CPU资源-2 -q 0,4,32 --shm-size 64
        inspire notebook create --workspace 分布式训练空间 --project CI-情境智能 \
          --image sandbox-base:latest --group H200-2号机房 -q 1,20,200 \
          --post-start-script scripts/notebook_setup.sh
        inspire notebook create --workspace 分布式训练空间 --project CI-情境智能 \
          --image sandbox-base:latest --group H200-2号机房 -q 1,20,200 \
          --node qb-prod-gpu1736
    """
    if post_start and post_start_script:
        raise click.UsageError("Use either --post-start or --post-start-script, not both.")

    project_explicit = bool(project)

    run_notebook_create(
        ctx,
        name=name,
        workspace=workspace,
        workspace_id=None,
        quota=quota,
        project=project,
        image=image,
        shm_size=shm_size,
        auto_stop=auto_stop,
        wait=wait,
        post_start=post_start,
        post_start_script=post_start_script,
        json_output=ctx.json_output,
        priority=priority,
        project_explicit=project_explicit,
        group=group,
        node=node,
        profile_name=profile_name,
    )


@click.command("stop")
@click.argument("notebook", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def stop_notebook_cmd(
    ctx: Context,
    notebook: str,
    workspace: str,
    pick: Optional[int],
) -> None:
    """Stop a running notebook instance.

    \b
    Examples:
        inspire notebook stop my-notebook --workspace 分布式训练空间
    """
    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list --workspace <workspace|all>",
    )
    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    base_url = get_base_url()
    try:
        workspace_id = resolve_workspace_operation_scope(
            workspace=workspace,
            session=session,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        base_url=base_url,
        identifier=notebook,
        json_output=ctx.json_output,
        workspace_ids=[workspace_id],
        pick=pick,
        require_live=True,
    )

    try:
        browser_api_module.stop_notebook(notebook_id=notebook_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to stop notebook: {scrub_raw_ids(e)}", EXIT_API_ERROR)
        return

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                public_operation(notebook, "stopped")
            )
        )
        return

    click.echo(human_formatter.format_mutation_success("Notebook", "stopped", notebook))


@click.command("delete")
@click.argument("notebook", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def delete_notebook_cmd(
    ctx: Context,
    notebook: str,
    workspace: str,
    yes: bool,
    pick: Optional[int],
) -> None:
    """Permanently delete a notebook instance.

    \b
    The instance disappears from the platform UI. This cannot be undone;
    if the notebook is still running, stop it first. The local cached SSH
    connection is NOT removed — run `inspire notebook connection forget <notebook>`
    to clean up.

    \b
    Examples:
        inspire notebook delete my-notebook --workspace 分布式训练空间
        inspire notebook delete my-notebook --workspace 分布式训练空间 --yes
    """
    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list --workspace <workspace|all>",
    )
    require_confirmation(
        ctx,
        yes=yes,
        prompt=(
            f"Permanently delete notebook '{scrub_raw_ids(notebook)}'? "
            "This cannot be undone."
        ),
        message="Notebook deletion requires confirmation.",
    )
    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    base_url = get_base_url()
    try:
        workspace_id = resolve_workspace_operation_scope(
            workspace=workspace,
            session=session,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        base_url=base_url,
        identifier=notebook,
        json_output=ctx.json_output,
        workspace_ids=[workspace_id],
        pick=pick,
        require_live=True,
    )

    try:
        browser_api_module.delete_notebook(notebook_id=notebook_id, session=session)
    except Exception as e:
        _handle_error(
            ctx, "APIError", f"Failed to delete notebook: {scrub_raw_ids(e)}", EXIT_API_ERROR
        )
        return
    forget_resource_identity(
        session=session,
        resource_type="notebook",
        resource_id=notebook_id,
        name=notebook,
        workspace_id=workspace_id,
        owner_scope="self",
    )

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                public_operation(notebook, "deleted")
            )
        )
        return

    click.echo(human_formatter.format_mutation_success("Notebook", "deleted", notebook))


@click.command("start")
@click.argument("notebook", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Wait for notebook to reach RUNNING status (still required for post-start actions)",
)
@click.option(
    "--post-start",
    type=str,
    default=None,
    help="Post-start action after RUNNING: none or a shell command",
)
@click.option(
    "--post-start-script",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    default=None,
    help="Local shell script to upload and run in the notebook after RUNNING",
)
@pass_context
def start_notebook_cmd(
    ctx: Context,
    notebook: str,
    workspace: str,
    pick: Optional[int],
    wait: bool,
    post_start: Optional[str],
    post_start_script: Optional[Path],
) -> None:
    """Start a stopped notebook instance.

    \b
    Examples:
        inspire notebook start ring-8h100-test --workspace 分布式训练空间
        inspire notebook start ring-8h100-test --workspace 分布式训练空间 --wait
        inspire notebook start ring-8h100-test --workspace 分布式训练空间 --post-start 'bash /workspace/setup.sh'
        inspire notebook start ring-8h100-test --workspace 分布式训练空间 --post-start-script scripts/notebook_setup.sh
        inspire notebook start ring-8h100-test --workspace 分布式训练空间 --post-start none
    """
    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list --workspace <workspace|all>",
    )
    if post_start and post_start_script:
        raise click.UsageError("Use either --post-start or --post-start-script, not both.")

    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    base_url = get_base_url()
    config = load_config(ctx)
    try:
        post_start_spec = resolve_notebook_post_start_spec(
            config=config,
            post_start=post_start,
            post_start_script=post_start_script,
        )
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
        return

    try:
        workspace_id = resolve_workspace_operation_scope(
            workspace=workspace,
            session=session,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        base_url=base_url,
        identifier=notebook,
        json_output=ctx.json_output,
        workspace_ids=[workspace_id],
        pick=pick,
        require_live=True,
    )

    try:
        browser_api_module.start_notebook(notebook_id=notebook_id, session=session)
    except Exception as e:
        _handle_error(
            ctx, "APIError", f"Failed to start notebook: {scrub_raw_ids(e)}", EXIT_API_ERROR
        )
        return

    if not ctx.json_output:
        click.echo(human_formatter.format_mutation_success("Notebook", "started", notebook))

    notebook_detail = None
    if wait or post_start_spec is not None:
        if not wait and post_start_spec is not None and not ctx.json_output:
            click.echo(NO_WAIT_POST_START_WARNING, err=True)
        if not ctx.json_output:
            click.echo("Waiting for notebook to reach RUNNING status...")
        try:
            notebook_detail = browser_api_module.wait_for_notebook_running(
                notebook_id=notebook_id,
                session=session,
                progress_callback=None if ctx.json_output else _print_notebook_wait_progress,
            )
            if not ctx.json_output:
                click.echo("Notebook is now RUNNING.")
        except NotebookFailedError as e:
            _handle_error(
                ctx,
                "NotebookFailed",
                f"Notebook failed to start: {scrub_raw_ids(e)}",
                EXIT_API_ERROR,
                hint=scrub_raw_ids(e.events) or "Check the platform Events tab for details.",
            )
            return
        except TimeoutError as e:
            _handle_error(
                ctx,
                "Timeout",
                f"Timed out waiting for notebook to reach RUNNING: {scrub_raw_ids(e)}",
                EXIT_API_ERROR,
            )
            return

    if notebook_detail and post_start_spec is not None:
        quota = notebook_detail.get("quota") or {}
        gpu_count = quota.get("gpu_count", 0) or 0
        maybe_run_post_start(
            notebook_id=notebook_id,
            session=session,
            post_start_spec=post_start_spec,
            gpu_count=gpu_count,
            json_output=ctx.json_output,
        )

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                public_operation(notebook, "started")
            )
        )
        return

@click.command("status")
@click.argument("notebook", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def notebook_status(
    ctx: Context,
    notebook: str,
    workspace: str,
    pick: Optional[int],
) -> None:
    """Get status of a notebook instance.

    \b
    Examples:
        inspire notebook status my-notebook --workspace 分布式训练空间
    """
    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list --workspace <workspace|all>",
    )
    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    base_url = get_base_url()

    try:
        workspace_id = resolve_workspace_operation_scope(
            workspace=workspace,
            session=session,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    try:
        data, _notebook_id, _workspace_id = (
            _run_notebook_operation_with_stale_handle_retry(
                ctx,
                session=session,
                base_url=base_url,
                identifier=notebook,
                json_output=ctx.json_output,
                workspace_ids=[workspace_id],
                pick=pick,
                operation=lambda notebook_id: browser_api_module.get_notebook_detail(
                    notebook_id, session=session
                ),
            )
        )
    except ValueError as e:
        message = str(e)
        # v1 surfaced a missing notebook as a transport 404; v2 answers 200
        # with `ResourceNotFound` in the envelope. Both map to the same
        # public error.
        if "API returned 404" in message or "ResourceNotFound" in message:
            _handle_error(
                ctx,
                "NotFound",
                f"Notebook instance '{notebook}' not found",
                EXIT_API_ERROR,
            )
        else:
            _handle_error(ctx, "APIError", message, EXIT_API_ERROR)
        return
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    notebook_detail = data if isinstance(data, dict) else {}
    public_detail = public_notebook(
        _with_workspace_display_name(notebook_detail, workspace),
        fallback_name=notebook,
    )
    if ctx.json_output:
        click.echo(json_formatter.format_json(public_detail))
    else:
        _print_notebook_detail(public_detail)
    return


@click.command("list")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME|all",
    help="Workspace name or 'all'.",
)
@click.option(
    "--status",
    "-s",
    multiple=True,
    metavar="STATUS",
    help="Filter by status (e.g. RUNNING, STOPPED). Repeatable.",
)
@click.option(
    "--keyword",
    "keyword",
    default="",
    metavar="KEYWORD",
    help="Filter by notebook name (keyword search)",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum notebooks to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every matching notebook.")
@pass_context
def list_notebooks(
    ctx: Context,
    workspace: Optional[str],
    status: tuple[str, ...],
    keyword: str,
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List notebook/interactive instances.

    \b
    Examples:
        inspire notebook list --workspace 分布式训练空间
        inspire notebook list --workspace 分布式训练空间 -n 10
        inspire notebook list --workspace 分布式训练空间 -s RUNNING
        inspire notebook list --workspace 分布式训练空间 -s RUNNING -s STOPPED
        inspire notebook list --workspace 分布式训练空间 --keyword my-notebook
        inspire notebook list --workspace GPU资源空间 -s RUNNING -n 5
        inspire notebook list --workspace all
        inspire --json notebook list --workspace all
    """
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )
    try:
        workspace_ids, _ = resolve_workspace_query_scope(
            workspace=workspace,
            session=session,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return

    base_url = get_base_url()

    user_ids = _try_get_current_user_ids(session, base_url=base_url)
    if not user_ids:
        _handle_error(
            ctx,
            "AuthenticationError",
            _current_user_lookup_failure_message(session),
            EXIT_API_ERROR,
        )
        return

    status_filter = [s.upper() for s in status] if status else []
    workspace_errors: dict[str, Exception] | None = {} if len(workspace_ids) > 1 else None

    try:
        workspace_items = _list_notebooks_for_workspaces(
            session,
            workspace_ids=workspace_ids,
            user_ids=user_ids,
            keyword=keyword,
            page_size=100,
            status=status_filter,
            errors=workspace_errors,
        )
    except ValueError:
        logger.debug("Notebook list validation/API response failed", exc_info=True)
        _handle_error(
            ctx,
            "APIError",
            "Could not list notebooks.",
            EXIT_API_ERROR,
            hint="Check auth and proxy configuration.",
        )
        return
    except Exception:
        logger.debug("Notebook list failed", exc_info=True)
        _handle_error(ctx, "APIError", "Could not list notebooks.", EXIT_API_ERROR)
        return

    all_items: list[dict] = []
    for ws_id in workspace_ids:
        workspace_name = _workspace_display(session, ws_id)
        workspace_rows = [
            _with_workspace_display_name(item, workspace_name)
            for item in workspace_items.get(ws_id, [])
        ]
        all_items.extend(_sort_notebook_items(workspace_rows))

    if workspace_errors and not ctx.json_output:
        for ws_id, error in workspace_errors.items():
            workspace_name = _workspace_display(session, ws_id)
            logger.debug(
                "Notebook list failed for workspace %s: %s",
                ws_id,
                error,
            )
            click.echo(
                f"Warning: workspace {scrub_raw_ids(workspace_name)} unavailable.",
                err=True,
            )

    if not all_items and len(workspace_ids) > 1:
        _handle_error(
            ctx,
            "APIError",
            "Failed to list notebooks from visible workspaces.",
            EXIT_API_ERROR,
        )
        return

    page = bound_collection(all_items, limit=effective_limit)
    _print_notebook_list(
        page.items,
        ctx.json_output,
        total=page.total,
        truncated=page.truncated,
    )
    if not ctx.json_output:
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)


__all__ = [
    "create_notebook_cmd",
    "list_notebooks",
    "notebook_status",
    "start_notebook_cmd",
    "stop_notebook_cmd",
]
