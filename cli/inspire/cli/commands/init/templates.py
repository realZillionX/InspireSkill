"""Template mode, smart mode, and config file writing for ``inspire init``."""

from __future__ import annotations

import os
from pathlib import Path

import click

from inspire.config import (
    DEFAULT_BASE_URL,
    Config,
    ConfigOption,
)

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


ACCOUNT_CONFIG_TEMPLATE = f"""# Inspire CLI Account Configuration
# Account-level values are independent of the current repository.
# Live project/resource catalogs are never copied here.
#
# Values here are overridden by environment variables.
# Sensitive values (passwords, tokens) should use env vars.

[auth]
username = "your_username"
# password - use INSPIRE_PASSWORD env var

[api]
base_url = "{DEFAULT_BASE_URL}"

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

[job]
# shm_size = 32
# auto_fault_tolerance = false
# fault_tolerance_max_retry = 10
# enable_notification = false

[notebook]
# post_start = "bash /workspace/setup.sh"

[remote_env]
# Environment variables exported before notebook commands and jobs run.
# Tip: use "$VARNAME" or "${{VARNAME}}" to pull from your *local* env at runtime.
# WANDB_API_KEY = "$WANDB_API_KEY"
# HF_TOKEN = "$HF_TOKEN"
"""


def _init_template_mode(
    force: bool,
) -> None:
    """Initialize the active account config with placeholders."""
    config_path = _require_writable_global_path()

    if config_path.exists() and not force:
        message = "Account configuration already exists."
        click.echo(click.style(message, fg="yellow"))
        if not click.confirm("\nOverwrite existing config?"):
            return

    _atomic_write_text(config_path, ACCOUNT_CONFIG_TEMPLATE)


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
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib

    generated = tomllib.loads(toml_content)
    existing = Config._load_toml(output_path) if output_path.exists() else {}
    from .discover import _sanitize_account_config
    from .toml_helpers import _toml_dumps

    merged = _sanitize_account_config(existing)
    for section, value in generated.items():
        if isinstance(value, dict) and isinstance(merged.get(section), dict):
            merged[section].update(value)
        else:
            merged[section] = value
    _atomic_write_text(output_path, _toml_dumps(merged))

def _init_smart_mode(
    detected: list[tuple[ConfigOption, str]],
    force: bool,
) -> None:
    """Initialize the active account config using detected env vars."""
    if not detected:
        return
    _write_single_file(
        detected,
        _require_writable_global_path(),
        force,
        "account",
    )
