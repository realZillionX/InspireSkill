"""Implementation for the `inspire init` command."""

from __future__ import annotations

from pathlib import Path

import click

from inspire.cli.context import (
    Context,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.accounts import (
    AccountError,
    create_account,
    current_account,
    ensure_inspire_home,
    list_accounts,
    normalize_environment,
    set_current_account,
    validate_name,
)
from inspire.cli.commands.account.add import (
    DEFAULT_BASE_URL,
    EXAMPLE_PROXY,
    _render_config as _render_account_config,
)
from inspire.cli.env_bootstrap import write_shared_project_env_file
from inspire.config import Config
from inspire.config.toml import _project_config_write_path

from .discover import _init_discover_mode
from .env_detect import _detect_env_vars
from .errors import run_init_action
from .json_report import emit_init_result, snapshot_paths
from .templates import _init_smart_mode, _init_template_mode


_NO_ACTIVE_ACCOUNT_MESSAGE = "No active account configured. Run `inspire account add` first."


def _stdin_is_interactive() -> bool:
    stream = click.get_text_stream("stdin")
    return bool(getattr(stream, "isatty", lambda: False)())


def _require_active_account_config_path() -> Path:
    """Return the active account config path, or fail fast with a direct error."""
    global_path = Config.writable_config_path()
    if global_path is None:
        raise ValueError(_NO_ACTIVE_ACCOUNT_MESSAGE)
    return global_path


def _get_config_paths() -> tuple[Path, Path]:
    """Writable paths for ``inspire init``.

    The first element always lands under the active account's directory
    (``~/.inspire/accounts/<name>/config.toml``), so ``init`` fails fast
    when no account is active instead of crashing later on a ``None`` path.
    """
    global_path = _require_active_account_config_path()
    project_path = _project_config_write_path()
    return global_path, project_path


def _bootstrap_first_account_if_needed(
    *,
    effective_json: bool,
    non_interactive: bool,
    cli_username: str | None,
    cli_base_url: str | None,
) -> bool:
    """Create the first account inline for interactive ``inspire init``.

    ``inspire init`` is now the first-run path, so making users detour into
    ``inspire account add`` is unnecessary when no account exists yet. If an
    account directory already exists but none is active, we keep the explicit
    error boundary so we don't guess which account to use.
    """
    if current_account():
        return False
    if list_accounts():
        raise ValueError(
            "No active account configured. Run `inspire account use <name>` first."
        )
    if effective_json or non_interactive:
        raise ValueError(
            "No active account configured. Run `inspire account add <name>` first; "
            "non-interactive init cannot prompt for credentials."
        )

    ensure_inspire_home()
    click.echo("No active account configured. Creating the first account.\n")

    while True:
        raw_name = click.prompt(
            "Account alias",
            default="",
            show_default=False,
            type=str,
        ).strip()
        if not raw_name:
            click.echo(click.style("Account alias is required.", fg="red"), err=True)
            continue
        try:
            account_name = validate_name(raw_name)
        except AccountError as err:
            click.echo(click.style(f"Invalid account alias: {err}", fg="red"), err=True)
            continue
        break

    if cli_username is None:
        username = click.prompt(
            "Platform login username (login ID, not display name)",
            default=account_name,
            show_default=True,
        )
    else:
        username = cli_username
    username = username.strip()
    if not username:
        raise ValueError("Username cannot be empty.")

    password = click.prompt(
        "Platform password",
        hide_input=True,
        confirmation_prompt="Confirm password",
    )

    if cli_base_url is None:
        base_url = click.prompt(
            "Inspire base URL",
            default=DEFAULT_BASE_URL,
            show_default=True,
        )
    else:
        base_url = cli_base_url

    click.echo(
        "Proxy must reach BOTH the public internet and *.sii.edu.cn. "
        f"Example if your Clash mixed port is 7897: {EXAMPLE_PROXY}"
    )
    proxy = click.prompt(
        "Proxy URL (leave empty for none)",
        default="",
        show_default=False,
    )

    content = _render_account_config(
        username=username,
        password=password,
        base_url=base_url.strip(),
        proxy=(proxy or "").strip(),
    )
    try:
        target = create_account(account_name, content)
        set_current_account(account_name)
    except AccountError as err:
        raise ValueError(str(err)) from err

    del target
    click.echo(f"Active account: {account_name}")
    normalize_environment(interactive=True, auto_install_playwright=True)
    return True


@click.command()
@click.option(
    "--scope",
    type=click.Choice(["project", "global"], case_sensitive=False),
    default="global",
    show_default=True,
    help="Select the discovery/config target scope.",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Overwrite existing files without prompting",
)
@click.option(
    "--template",
    "-t",
    "template_flag",
    is_flag=True,
    help="Create template with placeholders (skip env var detection)",
)
@click.option(
    "--no-discover",
    is_flag=True,
    help="Skip platform discovery and only write template/smart config.",
)
@click.option(
    "--username",
    "-u",
    default=None,
    help=(
        "Platform login username, such as phone, student ID, or email "
        "(not the display name). Used by discovery."
    ),
)
@click.option(
    "--base-url",
    default=None,
    help="Platform base URL (prompted if not configured). Used by discovery.",
)
@click.option(
    "--select-project",
    "select_project_name",
    default=None,
    help=(
        "Pick a project explicitly by name (skips the interactive "
        "prompt and the platform-heuristic guess). Used by discovery."
    ),
)
@click.option(
    "--env-file",
    default=None,
    help="Register a repo-wide dotenv file in shared project config (project scope only).",
)
@pass_context
def init(
    ctx: Context,
    scope: str,
    force: bool,
    template_flag: bool,
    no_discover: bool,
    username: str | None,
    base_url: str | None,
    select_project_name: str | None,
    env_file: str | None,
) -> None:
    """Initialize Inspire CLI configuration.

    Plain `inspire init` defaults to global scope: it logs in or uses the
    active account, discovers visible workspaces / projects / compute groups,
    then writes account-level catalogs and remote path aliases to
    ~/.inspire/accounts/<account>/config.toml.

    `--scope project` also discovers platform catalogs, then writes this
    repository's project context and path-alias overrides to
    ./.inspire/accounts/<account>/config.toml.
    `--env-file` records repo-wide dotenv loading in ./.inspire/config.toml.

    `--template` writes a placeholder config. `--no-discover` forces
    environment-variable detection / smart init into one config file instead
    of running discovery.

    Discovery writes account-scoped catalogs and default path aliases to the
    active account config. When `--scope project` is selected, it also writes
    this repository's context and path-alias overrides to the repo config.

    \b
    Prompted passwords are stored in global config for the selected account.

    Template/smart modes avoid writing secrets.

    Without discovery (`--no-discover`), if no environment variables are
    detected (or with --template), init creates a template config with
    placeholder values.

    Discovery creates path aliases such as `me`, `public`, `global-me`,
    `ssd.me`, `hdd.me`, and `qb-ilm2.me`; the top-level `me` points at the
    selected path tier, with `ssd` suggested for the path hot tier.

    \b
    Examples:
      inspire init
      inspire init --force
      inspire init --scope project
      inspire init --template --scope project
      inspire init --no-discover --scope project
      inspire init --no-discover --scope global
      inspire init --scope project --env-file .env
    """
    effective_json = ctx.json_output
    non_interactive = effective_json or not _stdin_is_interactive()
    warnings: list[str] = []

    scope_value = scope.lower()
    global_flag = scope_value == "global"
    project_flag = scope_value == "project"
    run_discovery = not template_flag and not no_discover

    def _warn(msg: str) -> None:
        warnings.append(msg)
        if not effective_json:
            click.echo(click.style(f"Warning: {msg}", fg="yellow"))

    def _register_env_file() -> Path | None:
        if not env_file:
            return None
        path = write_shared_project_env_file(env_file)
        if not effective_json:
            click.echo(click.style("Registered project env file.", fg="green"))
        return path

    try:
        if env_file and not project_flag:
            raise ValueError("--env-file is only supported with `inspire init --scope project`.")

        if run_discovery:
            _bootstrap_first_account_if_needed(
                effective_json=effective_json,
                non_interactive=non_interactive,
                cli_username=username,
                cli_base_url=base_url,
            )

        global_path, project_path = _get_config_paths()
        before = snapshot_paths(global_path, project_path)
        discover_target_paths = [global_path, project_path] if project_flag else [global_path]

        if not run_discovery and (username or base_url or select_project_name):
            _warn(
                "--username, --base-url, and --select-project are only effective with "
                "discovery and were ignored."
            )

        if run_discovery:
            if non_interactive and not force and any(
                path.exists() for path in discover_target_paths
            ):
                raise ValueError(
                    "Non-interactive discover updates require --force when config files "
                    "already exist."
                )

            run_init_action(
                _init_discover_mode,
                effective_json,
                force,
                scope=scope_value,
                cli_username=username,
                cli_base_url=base_url,
                cli_select_project=select_project_name,
                non_interactive=non_interactive,
                verbose=ctx.debug,
            )
            env_file_config_path = _register_env_file()
            if env_file_config_path is not None and env_file_config_path not in discover_target_paths:
                discover_target_paths.append(env_file_config_path)

            emit_init_result(
                scope=scope_value,
                target_paths=discover_target_paths,
                before=before,
                warnings=warnings,
                effective_json=effective_json,
            )
            return

        if template_flag:
            if non_interactive:
                target_path = global_path if global_flag else project_path
                if target_path.exists() and not force:
                    raise ValueError(
                        "Non-interactive init requires --force to overwrite configuration."
                    )
            run_init_action(
                _init_template_mode,
                effective_json,
                global_flag,
                project_flag,
                force,
                verbose=ctx.debug,
            )
            env_file_config_path = _register_env_file()
            target_paths = [global_path] if global_flag else [project_path]
            if env_file_config_path is not None and env_file_config_path not in target_paths:
                target_paths.append(env_file_config_path)
            emit_init_result(
                scope=scope_value,
                target_paths=target_paths,
                before=before,
                warnings=warnings,
                effective_json=effective_json,
            )
            return

        detected = _detect_env_vars()

        if detected:
            if non_interactive and not force:
                if global_flag and global_path.exists():
                    raise ValueError(
                        "Non-interactive init requires --force to overwrite configuration."
                    )
                if project_flag and project_path.exists():
                    raise ValueError(
                        "Non-interactive init requires --force to overwrite configuration."
                    )
                if (
                    not (global_flag or project_flag)
                    and (global_path.exists() or project_path.exists())
                ):
                    raise ValueError(
                        "Non-interactive init requires --force for auto-split overwrites."
                    )

            run_init_action(
                _init_smart_mode,
                effective_json,
                detected,
                global_flag,
                project_flag,
                force,
                verbose=ctx.debug,
            )
            target_paths = []
            if global_flag:
                has_global = any(opt.scope == "global" for opt, _ in detected)
                target_paths = [global_path] if has_global else []
            elif project_flag:
                has_project = any(opt.scope == "project" for opt, _ in detected)
                target_paths = [project_path] if has_project else []
            else:
                has_global = any(opt.scope == "global" for opt, _ in detected)
                has_project = any(opt.scope == "project" for opt, _ in detected)
                target_paths = []
                if has_global:
                    target_paths.append(global_path)
                if has_project:
                    target_paths.append(project_path)
            env_file_config_path = _register_env_file()
            if env_file_config_path is not None and env_file_config_path not in target_paths:
                target_paths.append(env_file_config_path)
            emit_init_result(
                scope=scope_value,
                target_paths=target_paths,
                before=before,
                warnings=warnings,
                effective_json=effective_json,
            )
            return

        if non_interactive:
            target_path = global_path if global_flag else project_path
            if target_path.exists() and not force:
                raise ValueError(
                    "Non-interactive init requires --force to overwrite configuration."
                )
        run_init_action(
            _init_template_mode,
            effective_json,
            global_flag,
            project_flag,
            force,
            verbose=ctx.debug,
        )
        env_file_config_path = _register_env_file()
        target_paths = [global_path] if global_flag else [project_path]
        if env_file_config_path is not None and env_file_config_path not in target_paths:
            target_paths.append(env_file_config_path)
        emit_init_result(
            scope=scope_value,
            target_paths=target_paths,
            before=before,
            warnings=warnings,
            effective_json=effective_json,
        )
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_GENERAL_ERROR)
    except SystemExit:
        raise
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


__all__ = ["init"]
