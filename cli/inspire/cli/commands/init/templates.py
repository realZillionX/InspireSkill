"""Template mode, smart mode, and config file writing for ``inspire init``."""

from __future__ import annotations

from pathlib import Path

import click

from inspire.config import (
    Config,
    ConfigOption,
)

from inspire.services.account.account_config import (
    atomic_write_text as _atomic_write_text,
    ACCOUNT_CONFIG_TEMPLATE,
)

from .env_detect import _generate_toml_content


def _require_writable_global_path() -> Path:
    global_path = Config.writable_config_path()
    if global_path is None:
        raise click.ClickException("No active account configured. Run `inspire account add` first.")
    return global_path


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

    _atomic_write_text(config_path, ACCOUNT_CONFIG_TEMPLATE, private=True)


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
    from inspire.services.account.account_config import toml_dumps as _toml_dumps

    merged = _sanitize_account_config(existing)
    for section, value in generated.items():
        if isinstance(value, dict) and isinstance(merged.get(section), dict):
            merged[section].update(value)
        else:
            merged[section] = value
    _atomic_write_text(output_path, _toml_dumps(merged), private=True)

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
