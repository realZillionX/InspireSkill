"""Account check command – validates account config and authentication."""

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
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import (
    Config,
    ConfigError,
    SOURCE_DEFAULT,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session
from inspire.platform.web.session.proxy import describe_effective_proxy_config

from .proxy_output import (
    format_effective_proxy_lines,
    public_effective_proxy_summary,
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


def _find_placeholder_host_issues(cfg: Config, sources: dict[str, str]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for field_name, env_var in _HOST_VALIDATION_FIELDS:
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
    lines.append("Path-only API prefixes are allowed.")
    return "\n".join(lines)


def _validate_required_credentials(cfg: Config) -> None:
    if not cfg.username or not cfg.password:
        raise ConfigError(
            "Missing platform credentials. Run `inspire account add <name>` to "
            "configure them; the active account's `[auth]` block is the only "
            "supported source."
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
    account_path: Path | None,
    project_path: Path | None,
) -> dict[str, object]:
    env_base_url = os.environ.get("INSPIRE_BASE_URL")
    return {
        "configured": bool(str(cfg.base_url or "").strip()),
        "source": sources.get("base_url", SOURCE_DEFAULT),
        "prefer_source": getattr(cfg, "prefer_source", "env"),
        "precedence": _describe_precedence(getattr(cfg, "prefer_source", "env")),
        "env_present": bool(env_base_url),
        "account_config_present": bool(account_path),
        "project_config_present": bool(project_path),
    }


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@click.command("check")
@click.option(
    "--details",
    is_flag=True,
    help="Show source precedence, proxy routing, and config-file presence.",
)
@pass_context
def check(ctx: Context, details: bool) -> None:
    """Check the active account's settings and platform authentication.

    Verifies required account settings, validates host-shaped values, and
    confirms the active account can authenticate to the platform.

    \b
    Examples:
        inspire account check
        inspire account check --details
        inspire --json account check
    """
    effective_json = ctx.json_output
    show_details = details

    try:
        cfg, sources = Config.from_files_and_env(
            require_credentials=False,
        )
        account_path, project_path = Config.get_config_paths()
        _validate_project_base_url_shape(project_path)

        placeholder_issues = _find_placeholder_host_issues(cfg, sources)
        if placeholder_issues:
            raise ConfigError(_format_placeholder_issue_message(placeholder_issues))

        _validate_required_credentials(cfg)
        effective_proxy = (
            public_effective_proxy_summary(
                describe_effective_proxy_config(base_url=cfg.base_url)
            )
            if show_details
            else None
        )

        auth_ok = True
        auth_error = None

        try:
            session = get_web_session()
            browser_api_module.get_current_user(session=session)
        except (SessionExpiredError, ValueError) as e:
            auth_ok = False
            auth_error = str(e)

        base_url_resolution = _build_base_url_resolution(cfg, sources, account_path, project_path)
        default_base_url_hint = None
        if base_url_resolution["source"] == SOURCE_DEFAULT:
            default_base_url_hint = (
                "Base URL is using default fallback. Set [api] base_url in "
                "the active account config or run inspire account add."
            )

        result: dict[str, object] = {
            "configured": True,
            "authenticated": auth_ok,
        }
        if show_details:
            result.update(
                {
                    "base_url_resolution": base_url_resolution,
                    "effective_proxy": effective_proxy,
                }
            )
            if default_base_url_hint:
                result["note"] = default_base_url_hint
            if auth_error:
                result["authentication_error"] = json_formatter.sanitize_text(
                    auth_error,
                    redact_paths=True,
                    redact_urls=True,
                    redact_platform_paths=True,
                )

        if effective_json:
            click.echo(json_formatter.format_json(result, success=auth_ok))
        else:
            click.echo(human_formatter.format_success("Configuration: OK"))
            if auth_ok:
                click.echo(human_formatter.format_success("Authentication: OK"))
            else:
                click.echo(human_formatter.format_error("Authentication: FAILED"))

            if show_details:
                click.echo(
                    "Source: "
                    f"{base_url_resolution['source']} "
                    f"({base_url_resolution['precedence']})"
                )
                click.echo(
                    "Config files: "
                    f"account={'yes' if account_path else 'no'} "
                    f"project={'yes' if project_path else 'no'}"
                )
                if default_base_url_hint:
                    click.echo(click.style(f"Note: {default_base_url_hint}", fg="yellow"))
                if effective_proxy is not None:
                    for line in format_effective_proxy_lines(effective_proxy):
                        click.echo(line)
                if auth_error:
                    click.echo(
                        "Authentication error: "
                        + json_formatter.sanitize_text(
                            auth_error,
                            redact_paths=True,
                            redact_urls=True,
                            redact_platform_paths=True,
                        )
                    )

        if not auth_ok:
            sys.exit(EXIT_AUTH_ERROR)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)
