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

from .env_detect import _generate_toml_content


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

[proxy]
# Proxy is OPTIONAL. Leave commented if your network can reach *.sii.edu.cn directly.
# Replace 7897 with your local Clash mixed port when needed.
# requests_http = "http://127.0.0.1:7897"
# requests_https = "http://127.0.0.1:7897"
# playwright = "http://127.0.0.1:7897"
# rtunnel = "http://127.0.0.1:7897"

[tunnel]
retries = 3
retry_pause = 2.0

[remote_env]
# Environment variables exported before notebook commands and jobs run for every repo.
# Tip: use "$VARNAME" or "${{VARNAME}}" to pull from your *local* env at runtime.
# WANDB_API_KEY = "$WANDB_API_KEY"
# HF_TOKEN = "$HF_TOKEN"
"""


PROJECT_CONFIG_TEMPLATE = """# Inspire CLI Project Configuration
# Project-level values live in this repository for the active account override.
# Repo-wide project settings, such as [cli].env_file, live in
# ./.inspire/config.toml.
# Account identity, API, and proxy settings belong in
# ~/.inspire/accounts/<account>/config.toml.
#
# Values here are overridden by environment variables.

[context]
# project = "CI-情境智能"

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

[job]
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
# Environment variables exported before notebook commands and jobs run in this repo.
# Tip: use "$VARNAME" or "${{VARNAME}}" to pull from your *local* env at runtime.
# WANDB_API_KEY = "$WANDB_API_KEY"
# HF_TOKEN = "$HF_TOKEN"
"""


def _init_template_mode(
    global_flag: bool,
    project_flag: bool,
    force: bool,
) -> None:
    """Initialize config using template with placeholders (template mode)."""
    global_path = _require_writable_global_path()
    if global_flag:
        config_path = global_path
        is_global = True
        label = "Account configuration"
    elif project_flag:
        config_path = _project_config_write_path()
        is_global = False
        label = "Project configuration"
    else:  # Internal callers must use the same explicit scope contract as Click.
        raise ValueError("Init requires either global or project scope.")

    if config_path.exists() and not force:
        message = f"{label} already exists."
        click.echo(click.style(message, fg="yellow"))
        if not click.confirm("\nOverwrite existing config?"):
            return

    template = ACCOUNT_CONFIG_TEMPLATE if is_global else PROJECT_CONFIG_TEMPLATE
    _atomic_write_text(config_path, template)


def _write_single_file(
    detected: list[tuple[ConfigOption, str]],
    output_path: Path,
    force: bool,
    dest_name: str,
) -> None:
    if output_path.exists() and not force:
        message = f"{dest_name.capitalize()} configuration already exists."
        click.echo(click.style(message, fg="yellow"))
        if not click.confirm("\nOverwrite existing config?"):
            return

    toml_content = _generate_toml_content(detected)

    _atomic_write_text(output_path, toml_content)

def _init_smart_mode(
    detected: list[tuple[ConfigOption, str]],
    global_flag: bool,
    project_flag: bool,
    force: bool,
) -> None:
    """Initialize config using detected env vars (smart mode)."""
    if global_flag:
        global_opts = [(opt, val) for opt, val in detected if opt.scope == "global"]
        if not global_opts:
            return
        _write_single_file(
            global_opts,
            _require_writable_global_path(),
            force,
            "account",
        )
    elif project_flag:
        project_opts = [(opt, val) for opt, val in detected if opt.scope == "project"]
        if not project_opts:
            return
        _write_single_file(
            project_opts,
            _project_config_write_path(),
            force,
            "project",
        )
    else:
        raise ValueError("Init requires either global or project scope.")
