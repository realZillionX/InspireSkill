"""Account check command – validates account config and authentication."""

from __future__ import annotations

from inspire.services.account.account_check import (
    find_placeholder_host_issues,
    format_placeholder_issue_message,
    validate_required_credentials,
)

import os
import sys
from pathlib import Path

import click

from inspire.cli.context import (
    Context,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter
from inspire.services.utils import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.services.utils.raw_ids import scrub_raw_ids
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_base_url_resolution(
    cfg: Config,
    sources: dict[str, str],
    account_path: Path | None,
) -> dict[str, object]:
    env_base_url = os.environ.get("INSPIRE_BASE_URL")
    return {
        "configured": bool(str(cfg.base_url or "").strip()),
        "source": sources.get("base_url", SOURCE_DEFAULT),
        "env_present": bool(env_base_url),
        "account_config_present": bool(account_path),
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
    from inspire.accounts import current_account

    effective_json = ctx.json_output
    show_details = details

    try:
        cfg, sources = Config.from_files_and_env(
            require_credentials=False,
        )
        active_account = scrub_raw_ids(current_account() or "") or None
        from inspire.config.load_account_layer import _resolve_account_config_path

        account_path = _resolve_account_config_path()

        placeholder_issues = find_placeholder_host_issues(cfg, sources)
        if placeholder_issues:
            raise ConfigError(format_placeholder_issue_message(placeholder_issues))

        validate_required_credentials(cfg)

        auth_ok = True
        auth_error = None
        try:
            session = get_web_session()
            # A health check must prove the session against the platform; the
            # cached identity used by normal owner-filtered lists is not enough.
            browser_api_module.get_current_user(session=session, refresh=True)
        except (SessionExpiredError, ValueError) as e:
            auth_ok = False
            auth_error = str(e)

        # A failed check has to say why without a second run: the reason the
        # login did not go through and the route the request actually took are
        # the two things you need, and asking for `--details` after the fact
        # means re-running against a platform that just refused you.
        verbose = show_details or not auth_ok
        effective_proxy = (
            public_effective_proxy_summary(
                describe_effective_proxy_config(base_url=cfg.base_url)
            )
            if verbose
            else None
        )

        base_url_resolution = _build_base_url_resolution(cfg, sources, account_path)
        default_base_url_hint = None
        if base_url_resolution["source"] == SOURCE_DEFAULT:
            default_base_url_hint = (
                "Base URL is using default fallback. Set [api] base_url in "
                "the active account config or run inspire account add."
            )

        result: dict[str, object] = {
            "account": active_account,
            "configured": True,
            "authenticated": auth_ok,
        }
        if verbose:
            result["effective_proxy"] = effective_proxy
            if auth_error:
                result["authentication_error"] = json_formatter.sanitize_text(
                    auth_error,
                    redact_paths=True,
                    redact_urls=True,
                    redact_platform_paths=True,
                )
        if show_details:
            result["base_url_resolution"] = base_url_resolution
            if default_base_url_hint:
                result["note"] = default_base_url_hint

        if effective_json:
            click.echo(json_formatter.format_json(result, success=auth_ok))
        else:
            click.echo(f"Account: {active_account or '-'}")
            click.echo(human_formatter.format_success("Configuration: OK"))
            if auth_ok:
                click.echo(human_formatter.format_success("Authentication: OK"))
            else:
                click.echo(human_formatter.format_error("Authentication: FAILED"))
            if show_details:
                click.echo(
                    "Source: "
                    f"{base_url_resolution['source']} (environment overrides account TOML)"
                )
                click.echo(
                    "Config file: " f"account={'yes' if account_path else 'no'}"
                )
                if default_base_url_hint:
                    click.echo(click.style(f"Note: {default_base_url_hint}", fg="yellow"))
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
            if effective_proxy is not None:
                for line in format_effective_proxy_lines(effective_proxy):
                    click.echo(line)

        if not auth_ok:
            sys.exit(EXIT_AUTH_ERROR)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)
