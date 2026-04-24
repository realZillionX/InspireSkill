"""Config check command – validates environment and authentication."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import click

from inspire.cli.context import (
    Context,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import (
    Config,
    ConfigError,
    SOURCE_DEFAULT,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PLACEHOLDER_HOSTS = {
    "api.example.com",
    "example.com",
    "example.org",
    "example.net",
}
_PLACEHOLDER_HOST_SUFFIXES = (
    ".example.com",
    ".example.org",
    ".example.net",
)
_HOST_VALIDATION_FIELDS = (
    ("base_url", "INSPIRE_BASE_URL"),
    ("github_server", "INSP_GITHUB_SERVER"),
    ("docker_registry", "INSPIRE_DOCKER_REGISTRY"),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _describe_precedence(prefer_source: str) -> str:
    if prefer_source == "toml":
        return "project TOML wins on conflict"
    return "env vars win on conflict (default)"


def _extract_hostname(value: str | None) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text or text.startswith("/"):
        return None

    if "://" in text:
        parsed = urlsplit(text)
        return parsed.hostname.lower() if parsed.hostname else None

    if text.startswith("//"):
        parsed = urlsplit(f"https:{text}")
        return parsed.hostname.lower() if parsed.hostname else None

    candidate = text.split("/", 1)[0].strip()
    if not candidate or " " in candidate:
        return None
    if "@" in candidate:
        candidate = candidate.rsplit("@", 1)[-1]
    if ":" in candidate:
        candidate = candidate.split(":", 1)[0]
    if "." not in candidate:
        return None
    return candidate.lower()


def _is_placeholder_host(host: str) -> bool:
    if host in _PLACEHOLDER_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in _PLACEHOLDER_HOST_SUFFIXES)


def _should_validate_host_field(cfg: Config, field_name: str) -> bool:
    if field_name == "github_server":
        return bool(cfg.github_repo or cfg.github_token)
    return True


def _find_placeholder_host_issues(cfg: Config, sources: dict[str, str]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for field_name, env_var in _HOST_VALIDATION_FIELDS:
        if not _should_validate_host_field(cfg, field_name):
            continue

        raw_value = getattr(cfg, field_name, None)
        if raw_value in (None, ""):
            continue

        value = str(raw_value)
        host = _extract_hostname(value)
        if not host:
            continue
        if not _is_placeholder_host(host):
            continue

        issues.append(
            {
                "field": field_name,
                "env_var": env_var,
                "value": value,
                "host": host,
                "source": sources.get(field_name, SOURCE_DEFAULT),
            }
        )
    return issues


def _format_placeholder_issue_message(issues: list[dict[str, str]]) -> str:
    lines = ["Placeholder host values detected in configuration:"]
    for issue in issues:
        lines.append(
            f"  - {issue['env_var']} ({issue['field']}): "
            f"{issue['value']} [source: {issue['source']}]"
        )
    lines.append("Use real host values in config files or environment variables.")
    lines.append("Path-only defaults such as /auth/token are allowed.")
    return "\n".join(lines)


def _validate_required_credentials(cfg: Config) -> None:
    if not cfg.username:
        raise ConfigError(
            "Missing username configuration.\n"
            "Set INSPIRE_USERNAME env var or add to config.toml:\n"
            "  [auth]\n"
            "  username = 'your_username'"
        )
    if not cfg.password:
        raise ConfigError(
            "Missing password configuration.\n"
            "Set INSPIRE_PASSWORD env var or add an account password in config.toml:\n"
            '  [accounts."your_username"]\n'
            "  password = 'your_password'"
        )


def _validate_required_registry(cfg: Config) -> None:
    if not cfg.docker_registry:
        raise ConfigError(
            "Missing docker registry configuration.\n"
            "Set INSPIRE_DOCKER_REGISTRY env var or add to config.toml:\n"
            "  [api]\n"
            "  docker_registry = 'your-registry.example.com'"
        )


def _validate_project_base_url_shape(project_path: Path | None) -> None:
    if not project_path or not project_path.exists():
        return

    try:
        project_raw = Config._load_toml(project_path)
    except Exception as e:
        raise ConfigError(f"Failed to read project config at {project_path}: {e}") from e

    if "base_url" in project_raw:
        raise ConfigError(
            f"Invalid project config at {project_path}.\n"
            "Found top-level `base_url`; this key must be under [api].\n"
            "Use:\n"
            "  [api]\n"
            "  base_url = 'https://your-inspire-host'"
        )


def _build_base_url_resolution(
    cfg: Config,
    sources: dict[str, str],
    global_path: Path | None,
    project_path: Path | None,
) -> dict[str, object]:
    env_base_url = os.environ.get("INSPIRE_BASE_URL")
    return {
        "value": cfg.base_url,
        "source": sources.get("base_url", SOURCE_DEFAULT),
        "prefer_source": getattr(cfg, "prefer_source", "env"),
        "precedence": _describe_precedence(getattr(cfg, "prefer_source", "env")),
        "env_present": bool(env_base_url),
        "global_config_path": str(global_path) if global_path else None,
        "project_config_path": str(project_path) if project_path else None,
    }


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@click.command("check")
@click.option(
    "--json",
    "json_output_local",
    is_flag=True,
    help="Output as JSON (machine-readable). Equivalent to top-level --json.",
)
@pass_context
def check_config(ctx: Context, json_output_local: bool) -> None:
    """Check environment configuration and API authentication.

    Verifies configuration (from files and environment) and attempts to
    authenticate with the Inspire API.
    """
    ctx.json_output = bool(ctx.json_output or json_output_local)
    effective_json = ctx.json_output

    try:
        cfg, sources = Config.from_files_and_env(
            require_credentials=False,
            require_target_dir=False,
        )
        global_path, project_path = Config.get_config_paths()
        _validate_project_base_url_shape(project_path)

        placeholder_issues = _find_placeholder_host_issues(cfg, sources)
        if placeholder_issues:
            raise ConfigError(_format_placeholder_issue_message(placeholder_issues))

        _validate_required_credentials(cfg)
        _validate_required_registry(cfg)

        auth_ok = True
        auth_error = None

        try:
            AuthManager.get_api(cfg)
        except AuthenticationError as e:
            auth_ok = False
            auth_error = str(e)

        base_url_resolution = _build_base_url_resolution(cfg, sources, global_path, project_path)
        default_base_url_hint = None
        if base_url_resolution["source"] == SOURCE_DEFAULT:
            default_base_url_hint = (
                "Base URL is using default fallback. Set [api] base_url in "
                "./.inspire/config.toml or export INSPIRE_BASE_URL."
            )

        result = {
            "username": cfg.username,
            "base_url": cfg.base_url,
            "target_dir": cfg.target_dir,
            "job_cache_path": cfg.get_expanded_cache_path(),
            "log_pattern": cfg.log_pattern,
            "timeout": cfg.timeout,
            "max_retries": cfg.max_retries,
            "retry_delay": cfg.retry_delay,
            "auth_ok": auth_ok,
            "base_url_resolution": base_url_resolution,
            "validation": {
                "placeholder_host_issues": placeholder_issues,
                "base_url_default_hint": default_base_url_hint,
            },
        }
        if auth_error:
            result["auth_error"] = auth_error

        if effective_json:
            click.echo(json_formatter.format_json(result, success=auth_ok))
        else:
            if auth_ok:
                click.echo(human_formatter.format_success("Configuration looks good"))
            else:
                click.echo(human_formatter.format_error("Authentication failed"))

            click.echo(f"\nUsername:     {cfg.username}")
            click.echo(f"Base URL:     {cfg.base_url}")
            click.echo(f"Target dir:   {cfg.target_dir or '(not set - required for logs)'}")
            click.echo(f"Log pattern:  {cfg.log_pattern}")
            click.echo(f"Job cache:    {cfg.get_expanded_cache_path()}")
            click.echo(f"Timeout:      {cfg.timeout}s")
            click.echo(f"Max retries:  {cfg.max_retries}")
            click.echo(f"Retry delay:  {cfg.retry_delay}s")
            click.echo("\nBase URL resolution:")
            click.echo(f"  Value:                {base_url_resolution['value']}")
            click.echo(f"  Source:               {base_url_resolution['source']}")
            click.echo(f"  Precedence:           {base_url_resolution['precedence']}")
            click.echo(
                "  INSPIRE_BASE_URL set: "
                f"{'yes' if base_url_resolution['env_present'] else 'no'}"
            )
            click.echo(
                "  Global config:        "
                f"{base_url_resolution['global_config_path'] or '(not found)'}"
            )
            click.echo(
                "  Project config:       "
                f"{base_url_resolution['project_config_path'] or '(not found)'}"
            )

            if default_base_url_hint:
                click.echo(click.style(f"  Note: {default_base_url_hint}", fg="yellow"))

            if auth_error:
                click.echo(f"\nDetails: {auth_error}")

        if not auth_ok:
            sys.exit(EXIT_AUTH_ERROR)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)
