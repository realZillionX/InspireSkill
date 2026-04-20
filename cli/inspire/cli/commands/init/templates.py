"""Template mode, smart mode, and config file writing for ``inspire init``."""

from __future__ import annotations

from pathlib import Path

import click

from inspire.config import (
    CONFIG_FILENAME,
    PROJECT_CONFIG_DIR,
    Config,
    ConfigOption,
)

from .env_detect import _format_preview_by_scope, _generate_toml_content

CONFIG_TEMPLATE = """# Inspire CLI Configuration
# Location: {location_comment}
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
# requests_http = "http://127.0.0.1:7897"
# requests_https = "http://127.0.0.1:7897"
# playwright = "http://127.0.0.1:7897"
# rtunnel = "http://127.0.0.1:7897"

[paths]
target_dir = "/shared/EBM_dev"
log_pattern = "training_master_*.log"
job_cache = "~/.inspire/jobs.json"
log_cache_dir = "~/.inspire/logs"

[github]
server = "https://github.com"
repo = "owner/repo"
# token - use INSP_GITHUB_TOKEN env var (falls back to GITHUB_TOKEN)
log_workflow = "retrieve_job_log.yml"
sync_workflow = "sync_code.yml"
bridge_workflow = "run_bridge_action.yml"
remote_timeout = 90

[sync]
default_remote = "origin"

[bridge]
action_timeout = 600
denylist = ["*.tmp", ".git/*"]

[workspaces]
# cpu = "ws-..."       # Default workspace (CPU jobs / notebooks)
# gpu = "ws-..."       # GPU workspace (H100/H200 jobs)
# internet = "ws-..."  # Internet-enabled GPU workspace (e.g. RTX 4090)
# special = "ws-..."   # Custom alias (use with --workspace special)

[job]
# project_id = "project-..."
# workspace_id = "ws-..."
# image = "pytorch:latest"
# priority = 10
# shm_size = 32  # Default shared memory (GiB) for notebooks; jobs use it when set

[notebook]
resource = "1xH200"
# image = "pytorch:latest"
# post_start = "bash /workspace/bootstrap.sh"  # none | shell command

[remote_env]
# Environment variables exported before remote commands run.
# Tip: use "$VARNAME" or "${{VARNAME}}" to pull from your *local* env at runtime.
# WANDB_API_KEY = "$WANDB_API_KEY"
# HF_TOKEN = "$HF_TOKEN"
"""


def _init_template_mode(global_flag: bool, project_flag: bool, force: bool) -> None:
    """Initialize config using template with placeholders (template mode)."""
    global_path = Config.resolve_global_config_path()
    if global_flag:
        config_dir = global_path.parent
        config_path = global_path
        location_comment = "~/.config/inspire/config.toml (global)"
    elif project_flag:
        config_path = Path.cwd() / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        config_dir = config_path.parent
        location_comment = "./.inspire/config.toml (project-specific)"
    else:
        click.echo("Where would you like to create the config?")
        click.echo("  [g] Global config (~/.config/inspire/config.toml)")
        click.echo("  [p] Project config (./.inspire/config.toml)")
        choice = click.prompt(
            "Choice", default="p", type=click.Choice(["g", "p"], case_sensitive=False)
        )

        if choice.lower() == "g":
            config_dir = global_path.parent
            config_path = global_path
            location_comment = "~/.config/inspire/config.toml (global)"
        else:
            config_path = Path.cwd() / PROJECT_CONFIG_DIR / CONFIG_FILENAME
            config_dir = config_path.parent
            location_comment = "./.inspire/config.toml (project-specific)"

    if config_path.exists() and not force:
        click.echo(click.style(f"Config file already exists: {config_path}", fg="yellow"))
        if not click.confirm("\nOverwrite existing config?"):
            click.echo("Aborted.")
            return

    config_dir.mkdir(parents=True, exist_ok=True)

    content = CONFIG_TEMPLATE.format(location_comment=location_comment)
    config_path.write_text(content)

    click.echo(click.style(f"Created {config_path}", fg="green"))

    click.echo("\nNext steps:")
    click.echo(f"  1. Edit {config_path} with your settings")
    click.echo("  2. Set INSPIRE_USERNAME and INSPIRE_PASSWORD environment variables")
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

    output_path.parent.mkdir(parents=True, exist_ok=True)

    output_path.write_text(toml_content)
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
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
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

    global_path = Config.resolve_global_config_path()
    project_path = Path.cwd() / PROJECT_CONFIG_DIR / CONFIG_FILENAME

    if global_flag:
        _write_single_file(detected, global_path, force, "global")
    elif project_flag:
        _write_single_file(detected, project_path, force, "project")
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
