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


def _get_config_paths() -> tuple[Path, Path]:
    """Writable paths for ``inspire init``.

    The first element lands under the active account's directory
    (``~/.inspire/accounts/<name>/config.toml``) when one is set, so
    ``init --discover`` writes to the file the loader actually reads.
    Falls back to the legacy global path for users without an account.
    """
    global_path = Config.writable_config_path()
    project_path = Path.cwd() / PROJECT_CONFIG_DIR / CONFIG_FILENAME
    return global_path, project_path


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
    "--discover",
    is_flag=True,
    help="Discover projects/workspaces and write per-account catalog",
)
@click.option(
    "--probe-shared-path",
    is_flag=True,
    help=(
        "Probe shared filesystem paths by SSHing into a small CPU notebook per project "
        "(slow; creates notebooks)."
    ),
)
@click.option(
    "--probe-limit",
    type=int,
    default=0,
    show_default=True,
    help=(
        "Limit number of projects to probe (0 = all). "
        "Only effective with --discover --probe-shared-path."
    ),
)
@click.option(
    "--probe-keep-notebooks",
    is_flag=True,
    help=(
        "Keep probe notebooks running (do not stop them after probing). "
        "Only effective with --discover --probe-shared-path."
    ),
)
@click.option(
    "--probe-pubkey",
    "--pubkey",
    "probe_pubkey",
    default=None,
    help=(
        "SSH public key path for probing (defaults to ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub). "
        "Only effective with --discover --probe-shared-path."
    ),
)
@click.option(
    "--probe-timeout",
    type=int,
    default=900,
    show_default=True,
    help=(
        "Per-project probe timeout in seconds. "
        "Only effective with --discover --probe-shared-path."
    ),
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
    help="Platform username (prompted if not configured). Only used with --discover.",
)
@click.option(
    "--base-url",
    default=None,
    help="Platform base URL (prompted if not configured). Only used with --discover.",
)
@click.option(
    "--target-dir",
    default=None,
    help="Target directory on shared filesystem (skips prompt). Only used with --discover.",
)
@pass_context
def init(
    ctx: Context,
    json_output_local: bool,
    global_flag: bool,
    project_flag: bool,
    force: bool,
    discover: bool,
    probe_shared_path: bool,
    probe_limit: int,
    probe_keep_notebooks: bool,
    probe_pubkey: str | None,
    probe_timeout: int,
    template_flag: bool,
    username: str | None,
    base_url: str | None,
    target_dir: str | None,
) -> None:
    """Initialize Inspire CLI configuration.

    Detects environment variables and creates config.toml files.
    By default, options are auto-split by scope: global options go to
    ~/.config/inspire/config.toml, project options go to ./.inspire/config.toml.

    Use --global or --project to force all options to a single file.
    Template/smart modes avoid writing secrets. In --discover mode, prompted
    account passwords are stored in global config for the selected account.

    If no environment variables are detected (or with --template), creates
    a template config with placeholder values.

    Use --discover to login via the web UI, discover accessible projects and
    compute groups, and write an account-scoped catalog to the global config.

    \b
    Examples:
        # Auto-detect env vars and split by scope
        inspire init

        \b
        # Force all options to global config
        inspire init --global

        \b
        # Force all options to project config
        inspire init --project

        \b
        # Create template with placeholders
        inspire init --template

        \b
        # Discover projects/workspaces and write per-account catalog
        inspire init --discover
    """
    ctx.json_output = bool(ctx.json_output or json_output_local)
    effective_json = ctx.json_output

    global_path, project_path = _get_config_paths()
    before = snapshot_paths(global_path, project_path)
    warnings: list[str] = []

    def _warn(msg: str) -> None:
        warnings.append(msg)
        if not effective_json:
            click.echo(click.style(f"Warning: {msg}", fg="yellow"))

    if not discover and (
        probe_limit or probe_keep_notebooks or probe_pubkey or probe_timeout != 900
    ):
        _warn(
            "Probe options are only effective with --discover --probe-shared-path and were ignored."
        )

    if not discover and (username or base_url or target_dir):
        _warn(
            "--username, --base-url, and --target-dir are only effective with --discover and were ignored."
        )

    try:
        if global_flag and project_flag:
            raise ValueError("Cannot specify both --global and --project")

        if discover:
            if template_flag:
                raise ValueError("Cannot combine --discover with --template")
            if global_flag or project_flag:
                raise ValueError("--discover always writes both global and project config")

            if not probe_shared_path and (
                probe_limit or probe_keep_notebooks or probe_pubkey or probe_timeout != 900
            ):
                _warn("Probe options require --probe-shared-path and were ignored.")

            if effective_json and not force and (global_path.exists() or project_path.exists()):
                raise ValueError(
                    "JSON mode is non-interactive for discover updates; rerun with --force when "
                    "config files already exist."
                )

            run_init_action(
                _init_discover_mode,
                effective_json,
                force,
                probe_shared_path=probe_shared_path,
                probe_limit=probe_limit,
                probe_keep_notebooks=probe_keep_notebooks,
                probe_pubkey=probe_pubkey,
                probe_timeout=probe_timeout,
                cli_username=username,
                cli_base_url=base_url,
                cli_target_dir=target_dir,
            )

            emit_init_json(
                mode="discover",
                target_paths=[global_path, project_path],
                before=before,
                detected=[],
                warnings=warnings,
                discover={
                    "probe_enabled": bool(probe_shared_path),
                    "probe_limit": int(probe_limit),
                    "probe_keep_notebooks": bool(probe_keep_notebooks),
                    "probe_timeout": int(probe_timeout),
                    "probe_pubkey_provided": bool(probe_pubkey),
                },
                effective_json=effective_json,
            )
            return

        if probe_shared_path:
            raise ValueError("--probe-shared-path requires --discover")

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
