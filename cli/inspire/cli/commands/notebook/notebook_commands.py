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
    get_base_url,
    load_config,
    require_web_session,
    resolve_json_output,
)
from inspire.cli.utils.notebook_post_start import (
    NO_WAIT_POST_START_WARNING,
    resolve_notebook_post_start_spec,
)
from inspire.cli.utils.tunnel_reconnect import rebuild_notebook_bridge_profile
from inspire.config import ConfigError
from inspire.config.ssh_runtime import resolve_ssh_runtime_config
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
        "resolve_ssh_runtime_config": resolve_ssh_runtime_config,
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


@click.command("create")
@click.option(
    "--name",
    "-n",
    help="Notebook name (auto-generated if omitted)",
)
@click.option(
    "--workspace",
    help="Workspace name (from [workspaces])",
)
@click.option(
    "--resource",
    "-r",
    default=None,
    help="Resource spec (e.g., 1xH200, 4xH100, 4CPU) (default from config [notebook].resource)",
)
@click.option(
    "--project",
    "-p",
    default=None,
    help="Project name (default from config [context].project; see 'inspire config context')",
)
@click.option(
    "--image",
    "-i",
    default=None,
    help=(
        "Image name/URL (default from config [notebook].image or [job].image; prompts interactively "
        "if still omitted)"
    ),
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
    "--auto/--no-auto",
    default=True,
    help="Auto-select best available compute group based on availability (default: auto)",
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
    "--keepalive/--no-keepalive",
    default=None,
    hidden=True,
    expose_value=False,
    help="Deprecated no-op option kept for backward compatibility",
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
    default=None,
    help="Task priority (1-10, default from config [job].priority or 10)",
)
@click.option(
    "--group",
    "group",
    default=None,
    help=(
        "Force a specific compute group by name (e.g. 'HPC-可上网区资源-2'). "
        "Bypasses the auto-selection heuristic. Partial matches accepted."
    ),
)
@pass_context
def create_notebook_cmd(
    ctx: Context,
    name: Optional[str],
    workspace: Optional[str],
    resource: Optional[str],
    project: Optional[str],
    image: Optional[str],
    shm_size: Optional[int],
    auto_stop: bool,
    auto: bool,
    wait: bool,
    post_start: Optional[str],
    post_start_script: Optional[Path],
    json_output: bool,
    priority: Optional[int],
    group: Optional[str],
) -> None:
    """Create a new interactive notebook instance.

    \b
    Examples:
        inspire notebook create                     # Interactive mode, auto-select GPU
        inspire notebook create -r 1xH200           # 1 GPU H200
        inspire notebook create -r 4xH100 -n mytest # 4 GPUs H100
        inspire notebook create -r 4x               # 4 GPUs, auto-select type
        inspire notebook create -r 8x               # 8 GPUs (full node), auto-select type
        inspire notebook create -r 4CPU             # 4 CPUs
        inspire notebook create -r 1xH100 --shm-size 64  # With 64GB shared memory
        inspire notebook create --no-auto -r 1xH200 # Disable auto-select
        inspire notebook create --post-start 'bash /workspace/bootstrap.sh'
        inspire notebook create --post-start-script scripts/notebook_bootstrap.sh
        inspire notebook create --post-start none --no-wait
        inspire notebook create --priority 5        # Set task priority to 5
    """
    if post_start and post_start_script:
        raise click.UsageError("Use either --post-start or --post-start-script, not both.")

    project_explicit = bool(project)

    run_notebook_create(
        ctx,
        name=name,
        workspace=workspace,
        workspace_id=None,
        resource=resource,
        project=project,
        image=image,
        shm_size=shm_size,
        auto_stop=auto_stop,
        auto=auto,
        wait=wait,
        keepalive=None,
        post_start=post_start,
        post_start_script=post_start_script,
        json_output=json_output,
        priority=priority,
        project_explicit=project_explicit,
        group=group,
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
        inspire notebook stop abc123-def456
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Stopping notebooks requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
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
        _handle_error(ctx, "APIError", f"Failed to stop notebook: {e}", EXIT_API_ERROR)
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

    click.echo(f"Notebook '{notebook_id}' is being stopped.")
    click.echo(f"Use 'inspire notebook status {notebook_id}' to check status.")


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
    """Permanently delete a notebook instance (Browser API).

    \b
    The instance disappears from the platform UI. This cannot be undone;
    if the notebook is still running, stop it first. Any saved local alias
    is NOT removed — run `inspire notebook forget <alias>` to clean up.

    \b
    Examples:
        inspire notebook delete abc123-def456
        inspire notebook delete nb-abc123de --yes
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Deleting notebooks requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
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
            f"Permanently delete notebook '{notebook_id}'? This cannot be undone.",
            abort=True,
        )

    try:
        result = browser_api_module.delete_notebook(notebook_id=notebook_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to delete notebook: {e}", EXIT_API_ERROR)
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

    click.echo(f"Notebook '{notebook_id}' deleted.")


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
    "--keepalive/--no-keepalive",
    default=None,
    hidden=True,
    expose_value=False,
    help="Deprecated no-op option kept for backward compatibility",
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
        inspire notebook start 78822a57-3830-44e7-8d45-e8b0d674fc44
        inspire notebook start ring-8h100-test
        inspire notebook start ring-8h100-test --wait
        inspire notebook start ring-8h100-test --post-start 'bash /workspace/bootstrap.sh'
        inspire notebook start ring-8h100-test --post-start-script scripts/notebook_bootstrap.sh
        inspire notebook start ring-8h100-test --post-start none
    """
    if post_start and post_start_script:
        raise click.UsageError("Use either --post-start or --post-start-script, not both.")

    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Starting notebooks requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
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
        _handle_error(ctx, "APIError", f"Failed to start notebook: {e}", EXIT_API_ERROR)
        return

    if not json_output:
        click.echo(f"Notebook '{notebook_id}' is being started.")

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
                f"Notebook failed to start: {e}",
                EXIT_API_ERROR,
                hint=e.events or "Check Events tab in web UI for details.",
            )
            return
        except TimeoutError as e:
            _handle_error(
                ctx,
                "Timeout",
                f"Timed out waiting for notebook to reach RUNNING: {e}",
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

    click.echo(f"Use 'inspire notebook status {notebook_id}' to check status.")


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
        inspire notebook status notebook-abc-123
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Notebook status requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
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
                f"Notebook instance '{notebook_id}' not found",
                EXIT_API_ERROR,
            )
        else:
            _handle_error(ctx, "APIError", message, EXIT_API_ERROR)
        return
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    if data.get("code") == 0:
        notebook = data.get("data", {})
        if json_output:
            click.echo(json_formatter.format_json(notebook))
        else:
            _print_notebook_detail(notebook)
        return

    _handle_error(
        ctx,
        "APIError",
        data.get("message", "Unknown error"),
        EXIT_API_ERROR,
    )
    return


@click.command("list")
@click.option(
    "--workspace",
    help="Workspace name (from [workspaces])",
)
@click.option(
    "--all",
    "-a",
    "show_all",
    is_flag=True,
    help="Show all notebooks (not just your own)",
)
@click.option(
    "--all-workspaces",
    "-A",
    is_flag=True,
    help="List notebooks across all configured workspaces (cpu/gpu/internet)",
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
    show_all: bool,
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
        inspire notebook list --all
        inspire notebook list -n 10
        inspire notebook list -s RUNNING
        inspire notebook list -s RUNNING -s STOPPED
        inspire notebook list --name my-notebook
        inspire notebook list --workspace gpu -s RUNNING -n 5
        inspire notebook list --all-workspaces
        inspire notebook list --json
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Listing notebooks requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )
    config = load_config(ctx)

    workspace_ids: list[str] = []
    if workspace:
        try:
            resolved = select_workspace_id(config, explicit_workspace_name=workspace)
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return
        if resolved:
            workspace_ids = [resolved]
    elif all_workspaces:
        candidates: list[str] = []
        for ws_id in (
            config.job_workspace_id,
        ):
            if ws_id:
                candidates.append(ws_id)
        if config.workspaces:
            candidates.extend(config.workspaces.values())
        if getattr(session, "workspace_id", None):
            candidates.append(str(session.workspace_id))

        workspace_ids = _unique_workspace_ids(candidates)
        for ws_id in workspace_ids:
            try:
                select_workspace_id(config, explicit_workspace_id=ws_id)
            except ConfigError as e:
                _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
                return

    if not workspace_ids:
        try:
            resolved = select_workspace_id(config)
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return

        resolved = resolved or getattr(session, "workspace_id", None)
        resolved = None if resolved == _ZERO_WORKSPACE_ID else resolved
        if not resolved:
            _handle_error(
                ctx,
                "ConfigError",
                "No workspace_id configured or provided.",
                EXIT_CONFIG_ERROR,
                hint=(
                    "Use set [workspaces].cpu/[workspaces].gpu in config.toml, "
                    "or set INSPIRE_WORKSPACE_ID."
                ),
            )
            return
        workspace_ids = [str(resolved)]

    base_url = get_base_url()

    user_ids = [] if show_all else _try_get_current_user_ids(session, base_url=base_url)

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
                click.echo(f"Warning: workspace {ws_id} failed: {e}", err=True)
            continue
        except Exception as e:
            if len(workspace_ids) == 1:
                _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
                return
            if not ctx.json_output:
                click.echo(f"Warning: workspace {ws_id} failed: {e}", err=True)
            continue

    if not all_items and len(workspace_ids) > 1:
        _handle_error(
            ctx,
            "APIError",
            "Failed to list notebooks from configured workspaces.",
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
    "--save-as",
    help=(
        "Custom alias name for this notebook's saved connection. Defaults to "
        "a sanitised form of the notebook's display name (falls back to "
        "nb-<id[:8]> if the name is empty or unusable). Used by subsequent "
        "'notebook exec/shell/scp' and by 'ssh <alias>' after 'inspire "
        "notebook ssh-config --install'."
    ),
)
@click.option(
    "--port",
    default=31337,
    show_default=True,
    help="rtunnel server listen port inside notebook",
)
@click.option(
    "--ssh-port",
    default=22222,
    show_default=True,
    help="sshd port inside notebook",
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
    help="Timeout in seconds for rtunnel setup to complete",
)
@pass_context
def ssh_notebook_cmd(
    ctx: Context,
    notebook: str,
    wait: bool,
    pubkey: Optional[str],
    save_as: Optional[str],
    port: int,
    ssh_port: int,
    command: Optional[str],
    command_timeout: Optional[int],
    debug_playwright: bool,
    setup_timeout: int,
) -> None:
    """SSH into a notebook instance via rtunnel ProxyCommand.

    Polymorphic:
      - ``inspire notebook ssh <notebook-id>``  bootstraps the rtunnel/SSH
        toolchain and automatically saves the connection as an alias
        (default ``notebook-<first-8-chars-of-id>``; override with ``--save-as``).
      - ``inspire notebook ssh <alias>``  reconnects to a previously bootstrapped
        notebook via the saved alias (no bootstrap cost, auto-rebuilds tunnel
        if it dropped).

    \b
    Examples:
        inspire notebook ssh <id>                 # bootstrap + save alias
        inspire notebook ssh <id> --save-as box1  # bootstrap with custom alias
        inspire notebook ssh box1                 # reconnect via alias
        inspire notebook ssh <id> --command "hostname"
    """
    # Polymorphic fast path: if the arg unambiguously matches a saved alias
    # (and does NOT look like a notebook-id), jump to the reconnect flow
    # (same as legacy `notebook shell --alias <alias>`). Any id-shaped input
    # stays on the bootstrap path so a saved alias cannot accidentally shadow
    # a real notebook-id.
    if not save_as and not command and not pubkey:
        from .notebook_lookup import _looks_like_notebook_id

        if not _looks_like_notebook_id(notebook):
            try:
                from inspire.bridge.tunnel import TunnelError, load_tunnel_config

                from .remote_shell import bridge_ssh as _reconnect

                _cfg = load_tunnel_config()
            except (FileNotFoundError, TunnelError, ImportError):
                _cfg = None

            if _cfg and notebook in _cfg.bridges:
                click.get_current_context().invoke(_reconnect, bridge=notebook)
                return

    run_notebook_ssh(
        ctx,
        notebook_id=notebook,
        wait=wait,
        pubkey=pubkey,
        save_as=save_as,
        port=port,
        ssh_port=ssh_port,
        command=command,
        command_timeout=command_timeout,
        debug_playwright=debug_playwright,
        setup_timeout=setup_timeout,
    )


__all__ = [
    "create_notebook_cmd",
    "list_notebooks",
    "notebook_status",
    "ssh_notebook_cmd",
    "start_notebook_cmd",
    "stop_notebook_cmd",
]
