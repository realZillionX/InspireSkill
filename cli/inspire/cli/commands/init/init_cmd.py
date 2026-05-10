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
    DEFAULT_PROXY_HINT,
    _render_config as _render_account_config,
)
from inspire.config import (
    CONFIG_FILENAME,
    PROJECT_CONFIG_DIR,
    Config,
)

from .discover import _init_discover_mode
from .env_detect import _detect_env_vars
from .errors import run_init_action
from .json_report import emit_init_json, snapshot_paths
from .templates import _init_smart_mode, _init_template_mode


_NO_ACTIVE_ACCOUNT_MESSAGE = "No active account configured. Run `inspire account add` first."


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
    project_path = Path.cwd() / PROJECT_CONFIG_DIR / CONFIG_FILENAME
    return global_path, project_path


def _bootstrap_first_account_if_needed(
    *,
    effective_json: bool,
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
    if effective_json:
        raise ValueError(
            "No active account configured. Run `inspire account add <name>` first; "
            "JSON init cannot prompt for credentials."
        )

    ensure_inspire_home()
    click.echo("No active account configured. Creating the first account.\n")

    while True:
        raw_name = click.prompt("Account alias", default="default", show_default=True)
        try:
            account_name = validate_name(raw_name)
        except AccountError as err:
            click.echo(click.style(f"Invalid account alias: {err}", fg="red"), err=True)
            continue
        break

    if cli_username is None:
        username = click.prompt(
            "Platform login username",
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
        f"Typical value: {DEFAULT_PROXY_HINT}"
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

    click.echo(f"Created account: {target}")
    click.echo(f"Active account: {account_name}")
    normalize_environment(interactive=True, auto_install_playwright=True)
    return True


@click.command()
@click.option(
    "--json",
    "json_output_local",
    is_flag=True,
    help="Output as JSON (machine-readable). Equivalent to top-level --json.",
)
@click.option(
    "--global",
    "-g",
    "global_flag",
    is_flag=True,
    help="Force all options to global config (~/.config/inspire/)",
)
@click.option(
    "--project",
    "-p",
    "project_flag",
    is_flag=True,
    help="Force all options to project config (./.inspire/)",
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
    "--username",
    "-u",
    default=None,
    help="Platform username (prompted if not configured). Used by plain init discovery.",
)
@click.option(
    "--base-url",
    default=None,
    help="Platform base URL (prompted if not configured). Used by plain init discovery.",
)
@click.option(
    "--select-project",
    "select_project_name",
    default=None,
    help=(
        "Pick a project explicitly by name (skips the interactive "
        "prompt and the platform-heuristic guess). Used by plain init discovery."
    ),
)
@pass_context
def init(
    ctx: Context,
    json_output_local: bool,
    global_flag: bool,
    project_flag: bool,
    force: bool,
    template_flag: bool,
    username: str | None,
    base_url: str | None,
    select_project_name: str | None,
) -> None:
    """Initialize Inspire CLI configuration.

    Plain `inspire init` logs in or uses the active account, discovers visible
    workspaces / projects / compute groups, asks which storage tier the `me`
    path alias should use, then writes account config plus this repository's
    ./.inspire/config.toml.

    Legacy non-discovery modes are still available. `--template` writes a
    placeholder config. `--global` or `--project` forces environment-variable
    detection / smart init into one config file instead of running discovery.

    Discovery writes account-scoped catalogs to the active account config and
    project-scoped context/path aliases to ./.inspire/config.toml.

    \b
    Prompted passwords are stored in global config for the selected account.

    Template/smart modes avoid writing secrets.

    Without discovery, if no environment variables are detected (or with
    --template), init creates a template config with placeholder values.

    Discovery also creates path aliases such as `me`, `public`, `global-me`,
    `ssd.me`, `hdd.me`, and `qb-ilm2.me`; the top-level `me` points at the
    selected path tier, with `ssd` suggested for the path hot tier.

    \b
    Examples:
        # Discover account/project/workspace catalog and path aliases
        inspire init

        \b
        # Refresh discovered config non-interactively where possible
        inspire init --force

        \b
        # Create a placeholder project config instead of discovery
        inspire init --template

        \b
        # Legacy: detect env vars and write only project/global config
        inspire init --project
        inspire init --global
    """
    ctx.json_output = bool(ctx.json_output or json_output_local)
    effective_json = ctx.json_output
    warnings: list[str] = []

    run_discovery = not template_flag and not global_flag and not project_flag

    def _warn(msg: str) -> None:
        warnings.append(msg)
        if not effective_json:
            click.echo(click.style(f"Warning: {msg}", fg="yellow"))

    try:
        bootstrapped_account = False
        if run_discovery:
            bootstrapped_account = _bootstrap_first_account_if_needed(
                effective_json=effective_json,
                cli_username=username,
                cli_base_url=base_url,
            )

        global_path, project_path = _get_config_paths()
        before = snapshot_paths(global_path, project_path)

        if not run_discovery and (username or base_url or select_project_name):
            _warn(
                "--username, --base-url, and --select-project are only effective with plain `inspire init` and were ignored."
            )

        if global_flag and project_flag:
            raise ValueError("Cannot specify both --global and --project")

        if run_discovery:
            if effective_json and not force and (global_path.exists() or project_path.exists()):
                raise ValueError(
                    "JSON mode is non-interactive for discover updates; rerun with --force when "
                    "config files already exist."
                )

            run_init_action(
                _init_discover_mode,
                effective_json,
                force,
                cli_username=username,
                cli_base_url=base_url,
                cli_select_project=select_project_name,
            )

            emit_init_json(
                mode="discover",
                target_paths=[global_path, project_path],
                before=before,
                detected=[],
                warnings=warnings,
                discover={
                    "bootstrapped_account": bootstrapped_account,
                },
                effective_json=effective_json,
            )
            return

        if template_flag:
            if effective_json:
                if not global_flag and not project_flag:
                    # Match interactive default choice for machine mode.
                    project_flag = True

                target_path = global_path if global_flag else project_path
                if target_path.exists() and not force:
                    raise ValueError(
                        "JSON mode is non-interactive for overwrites; rerun with --force."
                    )
            else:
                click.echo("Creating template config with placeholders.\n")

            run_init_action(_init_template_mode, effective_json, global_flag, project_flag, force)
            emit_init_json(
                mode="template",
                target_paths=[global_path] if global_flag else [project_path],
                before=before,
                detected=[],
                warnings=warnings,
                effective_json=effective_json,
            )
            return

        detected = _detect_env_vars()

        if detected:
            if effective_json and not force:
                if global_flag and global_path.exists():
                    raise ValueError(
                        "JSON mode is non-interactive for overwrites; rerun with --force."
                    )
                if project_flag and project_path.exists():
                    raise ValueError(
                        "JSON mode is non-interactive for overwrites; rerun with --force."
                    )
                if (
                    not global_flag
                    and not project_flag
                    and (global_path.exists() or project_path.exists())
                ):
                    raise ValueError(
                        "JSON mode is non-interactive for overwrite prompts in auto-split mode; "
                        "rerun with --force."
                    )

            run_init_action(
                _init_smart_mode, effective_json, detected, global_flag, project_flag, force
            )
            target_paths: list[Path]
            if global_flag:
                target_paths = [global_path]
            elif project_flag:
                target_paths = [project_path]
            else:
                has_global = any(opt.scope == "global" for opt, _ in detected)
                has_project = any(opt.scope == "project" for opt, _ in detected)
                target_paths = []
                if has_global:
                    target_paths.append(global_path)
                if has_project:
                    target_paths.append(project_path)
            emit_init_json(
                mode="smart",
                target_paths=target_paths,
                before=before,
                detected=detected,
                warnings=warnings,
                effective_json=effective_json,
            )
            return

        if effective_json:
            if not global_flag and not project_flag:
                project_flag = True
            target_path = global_path if global_flag else project_path
            if target_path.exists() and not force:
                raise ValueError("JSON mode is non-interactive for overwrites; rerun with --force.")
        else:
            click.echo("No environment variables detected. Creating template config.\n")

        run_init_action(_init_template_mode, effective_json, global_flag, project_flag, force)
        emit_init_json(
            mode="template",
            target_paths=[global_path] if global_flag else [project_path],
            before=before,
            detected=[],
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
