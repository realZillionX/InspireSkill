"""Notebook subcommands."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Optional

import click

from . import notebook_lookup as notebook_lookup_module
from . import notebook_ssh_flow as notebook_ssh_flow_module
from .notebook_create_flow import maybe_run_post_start, run_notebook_create
from .notebook_lookup import (
    _ZERO_WORKSPACE_ID,
    _collect_workspace_ids_for_lookup,
    _get_current_user_detail,
    _list_notebooks_for_workspace,
    _resolve_notebook_id as _lookup_resolve_notebook_id,
    _sort_notebook_items,
    _try_get_current_user_ids,
    _unique_workspace_ids,
    _validate_notebook_account_access,
)
from .notebook_presenters import _print_notebook_detail, _print_notebook_list
from .notebook_ssh_flow import load_ssh_public_key
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import (
    WEB_AUTH_HINT,
    get_base_url,
    load_config,
    require_web_session,
    resolve_json_output,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.notebook_post_start import (
    NO_WAIT_POST_START_WARNING,
    resolve_notebook_post_start_spec,
)
from inspire.cli.utils.tunnel_reconnect import rebuild_notebook_bridge_profile
from inspire.config import ConfigError, write_project_path_alias
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.platform.web.browser_api import NotebookFailedError


def _call_with_module_overrides(
    module, overrides: dict[str, object], func, *args, **kwargs
):  # noqa: ANN001, ANN002, ANN003
    original = {name: getattr(module, name) for name in overrides}
    for name, value in overrides.items():
        setattr(module, name, value)
    try:
        return func(*args, **kwargs)
    finally:
        for name, value in original.items():
            setattr(module, name, value)


def _notebook_lookup_overrides() -> dict[str, object]:
    return {
        "_handle_error": _handle_error,
        "_collect_workspace_ids_for_lookup": _collect_workspace_ids_for_lookup,
        "_get_current_user_detail": _get_current_user_detail,
        "_list_notebooks_for_workspace": _list_notebooks_for_workspace,
        "_try_get_current_user_ids": _try_get_current_user_ids,
        "_validate_notebook_account_access": _validate_notebook_account_access,
    }


def _notebook_ssh_overrides() -> dict[str, object]:
    return {
        "_handle_error": _handle_error,
        "require_web_session": require_web_session,
        "load_config": load_config,
        "_resolve_notebook_id": _resolve_notebook_id,
        "_get_current_user_detail": _get_current_user_detail,
        "_validate_notebook_account_access": _validate_notebook_account_access,
        "load_ssh_public_key": load_ssh_public_key,
        "rebuild_notebook_bridge_profile": rebuild_notebook_bridge_profile,
        "subprocess": subprocess,
    }


def _resolve_notebook_id(*args, **kwargs):  # noqa: ANN002, ANN003
    return _call_with_module_overrides(
        notebook_lookup_module,
        _notebook_lookup_overrides(),
        _lookup_resolve_notebook_id,
        *args,
        **kwargs,
    )


def run_notebook_ssh(*args, **kwargs):  # noqa: ANN002, ANN003
    return _call_with_module_overrides(
        notebook_ssh_flow_module,
        _notebook_ssh_overrides(),
        notebook_ssh_flow_module.run_notebook_ssh,
        *args,
        **kwargs,
    )


def _workspace_display(session, workspace_id: str) -> str:  # noqa: ANN001
    names = getattr(session, "all_workspace_names", None)
    if isinstance(names, dict):
        name = names.get(workspace_id)
        if name:
            return str(name)
    return "(workspace name unavailable)"


@click.command("create")
@click.option(
    "--name",
    "-n",
    required=True,
    help="Notebook name",
)
@click.option(
    "--workspace",
    help="Workspace name. Required unless supplied by --profile.",
)
@click.option(
    "--quota",
    "-q",
    default=None,
    help=(
        "Resource quota as 'gpu,cpu,mem' (mem in GiB). "
        "Example: '1,20,200' for 1 GPU + 20 CPU + 200 GiB. "
        "Use '0,4,32' for CPU-only. "
        "The triple must match a resource spec in the workspace (see 'inspire resources specs'); "
        "pass --group to disambiguate when multiple compute groups offer the same triple. "
        "Required unless supplied by --profile."
    ),
)
@click.option(
    "--project",
    "-p",
    help="Project name. Required unless supplied by --profile.",
)
@click.option(
    "--image",
    "-i",
    help="Image name or URL. Required unless supplied by --profile.",
)
@click.option(
    "--shm-size",
    type=int,
    default=None,
    help="Shared memory size in GB (default: INSPIRE_SHM_SIZE/job.shm_size, else 32)",
)
@click.option(
    "--auto-stop/--no-auto-stop",
    default=False,
    help="Auto-stop when idle",
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
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@click.option(
    "--priority",
    type=click.IntRange(1, 10),
    default=10,
    show_default=True,
    help=(
        "Task priority 1-10 (1-3=LOW preemptible, 4=NORMAL, 5-10=HIGH stable). "
        "Project quota may cap the requested value."
    ),
)
@click.option(
    "--group",
    "group",
    help="Compute group name. Required unless supplied by --profile. Partial matches accepted when unique.",
)
@click.option(
    "--profile",
    "profile_name",
    default=None,
    help="Notebook condition profile providing workspace/project/group/quota/image.",
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
    json_output: bool,
    priority: Optional[int],
    group: Optional[str],
    profile_name: Optional[str],
) -> None:
    """Create a new interactive notebook instance.

    \b
    Examples:
        inspire notebook create --workspace 分布式训练空间 --project CI-情境智能 \
          --image sandbox-base:latest --group H200 -q 1,20,200
        inspire notebook create --workspace CPU资源空间 --project CI-情境智能 \
          --image sandbox-base:latest --group CPU资源-2 -q 0,4,32 --shm-size 64
        inspire notebook create --workspace 分布式训练空间 --project CI-情境智能 \
          --image sandbox-base:latest --group H200 -q 1,20,200 \
          --post-start-script scripts/notebook_setup.sh
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
        json_output=json_output,
        priority=priority,
        project_explicit=project_explicit,
        group=group,
        profile_name=profile_name,
    )


@click.command("stop")
@click.argument("notebook")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def stop_notebook_cmd(
    ctx: Context,
    notebook: str,
    json_output: bool,
) -> None:
    """Stop a running notebook instance.

    \b
    Examples:
        inspire notebook stop my-notebook
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    base_url = get_base_url()
    config = load_config(ctx)
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=json_output,
    )

    try:
        result = browser_api_module.stop_notebook(notebook_id=notebook_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to stop notebook: {scrub_raw_ids(e)}", EXIT_API_ERROR)
        return

    if json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "notebook_id": notebook_id,
                    "status": "stopping",
                    "result": result,
                }
            )
        )
        return

    click.echo(f"Notebook '{scrub_raw_ids(notebook)}' is being stopped.")
    click.echo(f"Use `inspire notebook status {scrub_raw_ids(notebook)}` to check status.")


@click.command("delete")
@click.argument("notebook")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def delete_notebook_cmd(
    ctx: Context,
    notebook: str,
    yes: bool,
    json_output: bool,
) -> None:
    """Permanently delete a notebook instance.

    \b
    The instance disappears from the platform UI. This cannot be undone;
    if the notebook is still running, stop it first. The local cached SSH
    connection is NOT removed — run `inspire notebook forget <notebook>`
    to clean up.

    \b
    Examples:
        inspire notebook delete my-notebook
        inspire notebook delete my-notebook --yes
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    base_url = get_base_url()
    config = load_config(ctx)
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=json_output,
    )

    if not yes and not json_output:
        click.confirm(
            f"Permanently delete notebook '{scrub_raw_ids(notebook)}'? This cannot be undone.",
            abort=True,
        )

    try:
        result = browser_api_module.delete_notebook(notebook_id=notebook_id, session=session)
    except Exception as e:
        _handle_error(
            ctx, "APIError", f"Failed to delete notebook: {scrub_raw_ids(e)}", EXIT_API_ERROR
        )
        return

    if json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "notebook_id": notebook_id,
                    "status": "deleted",
                    "result": result,
                }
            )
        )
        return

    click.echo(f"Notebook '{scrub_raw_ids(notebook)}' deleted.")


@click.command("start")
@click.argument("notebook")
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
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def start_notebook_cmd(
    ctx: Context,
    notebook: str,
    wait: bool,
    post_start: Optional[str],
    post_start_script: Optional[Path],
    json_output: bool,
) -> None:
    """Start a stopped notebook instance.

    \b
    Examples:
        inspire notebook start ring-8h100-test
        inspire notebook start ring-8h100-test --wait
        inspire notebook start ring-8h100-test --post-start 'bash /workspace/setup.sh'
        inspire notebook start ring-8h100-test --post-start-script scripts/notebook_setup.sh
        inspire notebook start ring-8h100-test --post-start none
    """
    if post_start and post_start_script:
        raise click.UsageError("Use either --post-start or --post-start-script, not both.")

    json_output = resolve_json_output(ctx, json_output)

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

    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=json_output,
    )

    try:
        result = browser_api_module.start_notebook(notebook_id=notebook_id, session=session)
    except Exception as e:
        _handle_error(
            ctx, "APIError", f"Failed to start notebook: {scrub_raw_ids(e)}", EXIT_API_ERROR
        )
        return

    if not json_output:
        click.echo(f"Notebook '{scrub_raw_ids(notebook)}' is being started.")

    notebook_detail = None
    if wait or post_start_spec is not None:
        if not wait and post_start_spec is not None and not json_output:
            click.echo(NO_WAIT_POST_START_WARNING, err=True)
        if not json_output:
            click.echo("Waiting for notebook to reach RUNNING status...")
        try:
            notebook_detail = browser_api_module.wait_for_notebook_running(
                notebook_id=notebook_id, session=session
            )
            if not json_output:
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
            ctx,
            notebook_id=notebook_id,
            session=session,
            post_start_spec=post_start_spec,
            gpu_count=gpu_count,
            json_output=json_output,
        )

    if json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "notebook_id": notebook_id,
                    "status": "starting",
                    "result": result,
                }
            )
        )
        return

    click.echo(f"Use `inspire notebook status {scrub_raw_ids(notebook)}` to check status.")


@click.command("status")
@click.argument("notebook")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def notebook_status(
    ctx: Context,
    notebook: str,
    json_output: bool,
) -> None:
    """Get status of a notebook instance.

    \b
    Examples:
        inspire notebook status my-notebook
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    base_url = get_base_url()

    config = load_config(ctx)
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=json_output,
    )

    try:
        data = web_session_module.request_json(
            session,
            "GET",
            f"{base_url}/api/v1/notebook/{notebook_id}",
            headers={"Accept": "application/json"},
            timeout=30,
        )
    except ValueError as e:
        message = str(e)
        if "API returned 404" in message:
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

    if data.get("code") == 0:
        notebook_payload = data.get("data", {})
        notebook_detail = notebook_payload if isinstance(notebook_payload, dict) else {}
        if json_output:
            click.echo(json_formatter.format_json(notebook_detail))
        else:
            _print_notebook_detail(notebook_detail)
        return

    _handle_error(
        ctx,
        "APIError",
        data.get("message", "Unknown error"),
        EXIT_API_ERROR,
    )
    return


@click.command("id")
@click.argument("notebook")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def notebook_id_cmd(
    ctx: Context,
    notebook: str,
    json_output: bool,
) -> None:
    """Print the platform ID for a notebook name."""
    json_output = resolve_json_output(ctx, json_output)
    session = require_web_session(ctx, hint=WEB_AUTH_HINT)
    base_url = get_base_url()
    config = load_config(ctx)
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=json_output,
    )

    if json_output:
        click.echo(
            json_formatter.format_json({"name": notebook, "id": notebook_id}, allow_ids=True)
        )
    else:
        click.echo(notebook_id)


@click.command("list")
@click.option(
    "--workspace",
    help="Workspace name",
)
@click.option(
    "--all-workspaces",
    "-A",
    is_flag=True,
    help="List notebooks across all visible workspaces",
)
@click.option(
    "--limit",
    "-n",
    type=int,
    default=20,
    show_default=True,
    help="Max number of notebooks to show",
)
@click.option(
    "--status",
    "-s",
    multiple=True,
    help="Filter by status (e.g. RUNNING, STOPPED). Repeatable.",
)
@click.option(
    "--name",
    "keyword",
    default="",
    help="Filter by notebook name (keyword search)",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def list_notebooks(
    ctx: Context,
    workspace: Optional[str],
    all_workspaces: bool,
    limit: int,
    status: tuple[str, ...],
    keyword: str,
    json_output: bool,
) -> None:
    """List notebook/interactive instances.

    \b
    Examples:
        inspire notebook list
        inspire notebook list -n 10
        inspire notebook list -s RUNNING
        inspire notebook list -s RUNNING -s STOPPED
        inspire notebook list --name my-notebook
        inspire notebook list --workspace GPU资源空间 -s RUNNING -n 5
        inspire notebook list --all-workspaces
        inspire notebook list --json
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )
    config = load_config(ctx)

    workspace_ids: list[str] = []
    if workspace:
        try:
            resolved = select_workspace_id(config, explicit_workspace_name=workspace, session=session)
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return
        if resolved:
            workspace_ids = [resolved]
    elif all_workspaces:
        candidates: list[str] = []
        if getattr(session, "workspace_id", None):
            candidates.append(str(session.workspace_id))
        candidates.extend(str(wid) for wid in getattr(session, "all_workspace_ids", None) or [])

        workspace_ids = _unique_workspace_ids(candidates)
        for ws_id in workspace_ids:
            try:
                select_workspace_id(config, explicit_workspace_id=ws_id)
            except ConfigError as e:
                _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
                return

    if not workspace_ids:
        # No --workspace and no --all-workspaces: use the web session's
        # currently active workspace.
        resolved = getattr(session, "workspace_id", None)
        resolved = None if resolved == _ZERO_WORKSPACE_ID else resolved
        if not resolved:
            from inspire.config.workspaces import workspace_required_hint

            _handle_error(
                ctx,
                "ConfigError",
                "No workspace selected and the web session doesn't have one.",
                EXIT_CONFIG_ERROR,
                hint=workspace_required_hint(config),
            )
            return
        workspace_ids = [str(resolved)]

    base_url = get_base_url()

    user_ids = _try_get_current_user_ids(session, base_url=base_url)
    if not user_ids:
        _handle_error(
            ctx,
            "AuthenticationError",
            "Cannot determine the current user from the live web session.",
            EXIT_API_ERROR,
        )
        return

    all_items: list[dict] = []
    for ws_id in workspace_ids:
        status_filter = [s.upper() for s in status] if status else []
        try:
            items = _list_notebooks_for_workspace(
                session,
                base_url=base_url,
                workspace_id=ws_id,
                user_ids=user_ids,
                keyword=keyword,
                page_size=limit,
                status=status_filter,
            )
            all_items.extend(items)
        except ValueError as e:
            if len(workspace_ids) == 1:
                _handle_error(
                    ctx,
                    "APIError",
                    str(e),
                    EXIT_API_ERROR,
                    hint="Check auth and proxy configuration.",
                )
                return
            if not ctx.json_output:
                click.echo(
                    f"Warning: workspace {_workspace_display(session, ws_id)} failed: {scrub_raw_ids(e)}",
                    err=True,
                )
            continue
        except Exception as e:
            if len(workspace_ids) == 1:
                _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
                return
            if not ctx.json_output:
                click.echo(
                    f"Warning: workspace {_workspace_display(session, ws_id)} failed: {scrub_raw_ids(e)}",
                    err=True,
                )
            continue

    if not all_items and len(workspace_ids) > 1:
        _handle_error(
            ctx,
            "APIError",
            "Failed to list notebooks from visible workspaces.",
            EXIT_API_ERROR,
        )
        return

    all_items = _sort_notebook_items(all_items)
    _print_notebook_list(all_items, json_output)


@click.command("ssh")
@click.argument("notebook")
@click.option(
    "--wait/--no-wait",
    default=True,
    help="Wait for notebook to reach RUNNING status",
)
@click.option(
    "--pubkey",
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help=(
        "SSH public key path to authorize (defaults to ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub)"
    ),
)
@click.option(
    "--port",
    default=31337,
    show_default=True,
    help="Advanced: connection service port inside notebook",
)
@click.option(
    "--ssh-port",
    default=22222,
    show_default=True,
    help="Advanced: SSH service port inside notebook",
)
@click.option(
    "--command",
    help=(
        "Optional non-interactive remote command to run " "(if omitted, opens an interactive shell)"
    ),
)
@click.option(
    "--command-timeout",
    type=int,
    default=None,
    help="Timeout in seconds for --command execution (default: 300, 0 disables)",
)
@click.option(
    "--debug-playwright",
    is_flag=True,
    help="Run browser automation with visible window for debugging",
)
@click.option(
    "--timeout",
    "setup_timeout",
    default=300,
    show_default=True,
    help="Timeout in seconds for notebook connection setup",
)
@pass_context
def ssh_notebook_cmd(
    ctx: Context,
    notebook: str,
    wait: bool,
    pubkey: Optional[str],
    port: int,
    ssh_port: int,
    command: Optional[str],
    command_timeout: Optional[int],
    debug_playwright: bool,
    setup_timeout: int,
) -> None:
    """Open an SSH session to a notebook instance.

    The positional argument is the notebook name. The first call establishes
    and caches the connection under that same name; later calls reconnect
    automatically. One notebook keeps one cached SSH connection — there is no
    separate alias concept.

    \b
    Examples:
        inspire notebook ssh <notebook-name>
        inspire notebook ssh <notebook-name> --command "hostname"
    """
    from inspire.accounts import normalize_environment

    normalize_environment()

    # Fast path: if a cached bridge already exists for this notebook
    # name, hand off to the reconnect flow (no bootstrap needed).
    if not command and not pubkey:
        try:
            from inspire.bridge.tunnel import TunnelError, load_tunnel_config

            from .remote_shell import bridge_ssh as _reconnect

            _cfg = load_tunnel_config()
        except (FileNotFoundError, TunnelError, ImportError):
            _cfg = None

        if _cfg and notebook in _cfg.bridges:
            click.get_current_context().invoke(_reconnect, notebook=notebook)
            return

    run_notebook_ssh(
        ctx,
        notebook_id=notebook,
        wait=wait,
        pubkey=pubkey,
        port=port,
        ssh_port=ssh_port,
        command=command,
        command_timeout=command_timeout,
        debug_playwright=debug_playwright,
        setup_timeout=setup_timeout,
    )


@click.command("set-path")
@click.argument("notebook")
@click.argument("remote_path")
@click.argument("as_keyword")
@click.argument("alias")
@pass_context
def set_path_alias_cmd(
    ctx: Context,
    notebook: str,
    remote_path: str,
    as_keyword: str,
    alias: str,
) -> None:
    """Store a project-level remote path alias for notebook commands.

    \b
    Example:
        inspire notebook set-path my-notebook /inspire/ssd/project/topic/user as me
        inspire notebook exec my-notebook --cwd me "pwd"
        inspire notebook scp my-notebook ./out.tar me:out.tar
    """
    from inspire.cli.utils.id_resolver import reject_id_at_boundary

    _ = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook connections",
    )
    if as_keyword != "as":
        raise click.UsageError("Expected syntax: inspire notebook set-path <name> <path> as <alias>")

    try:
        config_path = write_project_path_alias(alias=alias, remote_path=remote_path)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "alias": alias,
                    "path": remote_path,
                    "config_path": str(config_path),
                }
            )
        )
        return

    click.echo(f"Path alias '{alias}' = {remote_path}")
    click.echo(f"Updated {config_path}")


__all__ = [
    "create_notebook_cmd",
    "list_notebooks",
    "notebook_status",
    "set_path_alias_cmd",
    "ssh_notebook_cmd",
    "start_notebook_cmd",
    "stop_notebook_cmd",
]
