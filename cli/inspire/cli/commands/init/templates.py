"""Template mode, smart mode, and config file writing for ``inspire init``."""

from __future__ import annotations

import os
from pathlib import Path

import click

from inspire.config import (
    Config,
    ConfigOption,
)
from inspire.config.toml import _project_config_write_path

from .env_detect import _format_preview_by_scope, _generate_toml_content


def _atomic_write_text(target: Path, content: str) -> None:
    """Write *content* to *target* atomically (same-dir temp + ``os.replace``).

    ``inspire init`` writes config.toml files users will later edit by hand.
    A half-written config would be worse than a missed write, so fsync to
    disk before renaming over the target.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


def _require_writable_global_path() -> Path:
    global_path = Config.writable_config_path()
    if global_path is None:
        raise click.ClickException("No active account configured. Run `inspire account add` first.")
    return global_path


ACCOUNT_CONFIG_TEMPLATE = """# Inspire CLI Account Configuration
# Location: {location_comment}
#
# Account-level values are shared by every repository that uses this account.
# `inspire init` discovery may also write account-level default path aliases
# here. Repo-wide project settings live in ./.inspire/config.toml; account-
# specific project overrides such as personal path aliases live in
# ./.inspire/accounts/<account>/config.toml.
#
# Values here are overridden by environment variables.
# Sensitive values (passwords, tokens) should use env vars.

[auth]
username = "your_username"
# password - use INSPIRE_PASSWORD env var

[api]
base_url = "https://api.example.com"
timeout = 30
max_retries = 3
retry_delay = 1.0

[proxy]
# Proxy is OPTIONAL. Leave commented if your network can reach *.sii.edu.cn directly.
# Replace 7897 with your local Clash mixed port when needed.
# requests_http = "http://127.0.0.1:7897"
# requests_https = "http://127.0.0.1:7897"
# playwright = "http://127.0.0.1:7897"
# rtunnel = "http://127.0.0.1:7897"

[paths]
log_cache_dir = "~/.inspire/logs"

[github]
server = "https://github.com"
# token - use INSP_GITHUB_TOKEN env var (falls back to GITHUB_TOKEN)

[bridge]
action_timeout = 600

[tunnel]
retries = 3
retry_pause = 2.0

[remote_env]
# Environment variables exported before remote commands run for every repo.
# Tip: use "$VARNAME" or "${{VARNAME}}" to pull from your *local* env at runtime.
# WANDB_API_KEY = "$WANDB_API_KEY"
# HF_TOKEN = "$HF_TOKEN"
"""


PROJECT_CONFIG_TEMPLATE = """# Inspire CLI Project Configuration
# Location: {location_comment}
#
# Project-level values live in this repository for the active account override.
# Repo-wide project settings, such as [cli].env_file, live in
# ./.inspire/config.toml.
# Account identity, API, and proxy settings belong in
# ~/.inspire/accounts/<account>/config.toml.
#
# Values here are overridden by environment variables.

[context]
# project = "CI-情境智能"

[paths]
log_pattern = "training_master_*.log"

[path_aliases]
# Remote path aliases for notebook exec/shell/scp. Plain `inspire init` writes
# account-level defaults; `inspire init --scope project` writes repo overrides.
# <path-user> is the shared-storage personal directory segment reported by
# the platform, which can differ from the login username.
# me = "/inspire/ssd/project/<topic>/<path-user>/"
# public = "/inspire/ssd/project/<topic>/public/"
# global-me = "/inspire/ssd/global_user/<path-user>/"
# hdd.me = "/inspire/hdd/project/<topic>/<path-user>/"
# ssd.public = "/inspire/ssd/project/<topic>/public/"
# qb-ilm2.me = "/inspire/qb-ilm2/project/<topic>/<path-user>/"

[github]
repo = "owner/repo"
sync_workflow = "sync_code.yml"
bridge_workflow = "run_bridge_action.yml"
remote_timeout = 90

[sync]
default_remote = "origin"

[bridge]
denylist = ["*.tmp", ".git/*"]

[job]
# priority = 10
# shm_size = 32  # Default shared memory (GiB) for notebooks; jobs use it when set
# auto_fault_tolerance = false
# fault_tolerance_max_retry = 10
# enable_notification = false  # Feishu status updates to the current user's bound account

[notebook]
# post_start = "bash /workspace/setup.sh"  # none | shell command

[profiles.notebook.example]
# Workload condition profile used only when passed as --profile example.
# workspace = "分布式训练空间"
# project = "CI-情境智能"
# group = "H200-2号机房"
# quota = "1,20,200"
# image = "unified-base:v2"

[remote_env]
# Environment variables exported before remote commands run in this repo.
# Tip: use "$VARNAME" or "${{VARNAME}}" to pull from your *local* env at runtime.
# WANDB_API_KEY = "$WANDB_API_KEY"
# HF_TOKEN = "$HF_TOKEN"
"""


def _init_template_mode(global_flag: bool, project_flag: bool, force: bool) -> None:
    """Initialize config using template with placeholders (template mode)."""
    global_path = _require_writable_global_path()
    is_global = False
    if global_flag:
        config_path = global_path
        location_comment = f"{global_path} (account)"
        is_global = True
    elif project_flag:
        config_path = _project_config_write_path()
        location_comment = f"{config_path} (project/account)"
    else:
        click.echo("Where would you like to create the config?")
        click.echo("  [g] Account config (~/.inspire/accounts/<name>/config.toml)")
        click.echo("  [p] Project config (this repo, active account)")
        choice = click.prompt(
            "Choice", default="p", type=click.Choice(["g", "p"], case_sensitive=False)
        )

        if choice.lower() == "g":
            config_path = global_path
            location_comment = f"{global_path} (account)"
            is_global = True
        else:
            config_path = _project_config_write_path()
            location_comment = f"{config_path} (project/account)"

    if config_path.exists() and not force:
        click.echo(click.style(f"Config file already exists: {config_path}", fg="yellow"))
        if not click.confirm("\nOverwrite existing config?"):
            click.echo("Aborted.")
            return

    template = ACCOUNT_CONFIG_TEMPLATE if is_global else PROJECT_CONFIG_TEMPLATE
    content = template.format(location_comment=location_comment)
    _atomic_write_text(config_path, content)

    click.echo(click.style(f"Created {config_path}", fg="green"))

    click.echo("\nNext steps:")
    if is_global:
        click.echo(f"  1. Edit {config_path} with your account-level settings")
        click.echo("  2. Run 'inspire init' to discover catalogs and default path aliases")
        click.echo("  3. Run 'inspire init --scope project' in a repo for overrides")
        click.echo("  4. Run 'inspire config show' to verify your configuration")
    else:
        click.echo(f"  1. Edit {config_path} with your project-level settings")
        click.echo("  2. Run 'inspire account add <name>' if you have not configured credentials")
        click.echo("  3. Run 'inspire config show' to verify your configuration")


def _show_next_steps(detected: list[tuple[ConfigOption, str]]) -> None:
    secrets = [opt for opt, _ in detected if opt.secret]

    click.echo(click.style("Next steps:", bold=True))
    step = 1
    if secrets:
        secret_vars = ", ".join(opt.env_var for opt in secrets)
        click.echo(f"  {step}. Keep {secret_vars} as env var(s) (not written for security)")
        step += 1
    click.echo(f"  {step}. Verify with: inspire config show")


def _write_single_file(
    detected: list[tuple[ConfigOption, str]],
    output_path: Path,
    force: bool,
    dest_name: str,
) -> None:
    _ = dest_name

    if output_path.exists() and not force:
        click.echo(click.style(f"Config file already exists: {output_path}", fg="yellow"))
        if not click.confirm("\nOverwrite existing config?"):
            click.echo("Aborted.")
            return

    toml_content = _generate_toml_content(detected)

    _atomic_write_text(output_path, toml_content)
    click.echo(click.style(f"Created {output_path}", fg="green"))
    click.echo()

    _show_next_steps(detected)


def _write_auto_split(
    detected: list[tuple[ConfigOption, str]],
    global_opts: list[tuple[ConfigOption, str]],
    project_opts: list[tuple[ConfigOption, str]],
    global_path: Path,
    project_path: Path,
    force: bool,
    secrets: list[ConfigOption],
) -> None:
    _ = secrets

    files_to_write: list[tuple[str, Path]] = []

    if global_opts:
        if global_path.exists() and not force:
            click.echo(f"Global config already exists: {global_path}")
            if click.confirm("Overwrite?", default=False):
                files_to_write.append(("global", global_path))
            else:
                click.echo("Skipping global config.")
            click.echo()
        else:
            files_to_write.append(("global", global_path))

    if project_opts:
        if project_path.exists() and not force:
            click.echo(f"Project config already exists: {project_path}")
            if click.confirm("Overwrite?", default=False):
                files_to_write.append(("project", project_path))
            else:
                click.echo("Skipping project config.")
            click.echo()
        else:
            files_to_write.append(("project", project_path))

    if not files_to_write:
        click.echo("No files written.")
        return

    for scope, path in files_to_write:
        content = _generate_toml_content(detected, scope_filter=scope)
        _atomic_write_text(path, content)
        color = "cyan" if scope == "global" else "green"
        click.echo(click.style(f"Created {path}", fg=color))

    click.echo()
    _show_next_steps(detected)


def _init_smart_mode(
    detected: list[tuple[ConfigOption, str]],
    global_flag: bool,
    project_flag: bool,
    force: bool,
) -> None:
    """Initialize config using detected env vars (smart mode)."""
    _format_preview_by_scope(detected)

    secrets = [opt for opt, _ in detected if opt.secret]
    non_secrets = [(opt, val) for opt, val in detected if not opt.secret]
    global_opts = [(opt, val) for opt, val in detected if opt.scope == "global"]
    project_opts = [(opt, val) for opt, val in detected if opt.scope == "project"]

    click.echo(f"Found {len(detected)} environment variable(s):")
    click.echo(f"  - {len(non_secrets)} regular value(s)")
    if secrets:
        click.echo(f"  - {len(secrets)} secret(s) (excluded)")
    if not global_flag and not project_flag:
        click.echo(f"  - {len(global_opts)} global-scope option(s)")
        click.echo(f"  - {len(project_opts)} project-scope option(s)")
    click.echo()

    global_path = _require_writable_global_path()
    project_path = _project_config_write_path()

    if global_flag:
        if not global_opts:
            click.echo("No global-scope environment variables detected. No files written.")
            return
        _write_single_file(global_opts, global_path, force, "global")
    elif project_flag:
        if not project_opts:
            click.echo("No project-scope environment variables detected. No files written.")
            return
        _write_single_file(project_opts, project_path, force, "project")
    else:
        _write_auto_split(
            detected,
            global_opts,
            project_opts,
            global_path,
            project_path,
            force,
            secrets,
        )
