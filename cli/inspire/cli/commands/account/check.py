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
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.services.utils import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.config import (
    Config,
    ConfigError,
    SOURCE_DEFAULT,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, TransientAPIError, get_web_session
from inspire.platform.web.session.proxy import describe_effective_proxy_config

from inspire.services.account.auth_status import (
    inspect_auth_status,
    legacy_message,
    format_auth_status,
    login_hint,
)
from inspire.platform.web.session.cas import CasSession, cas_ticket_valid

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
@click.option(
    "--offline",
    is_flag=True,
    help="Inspect local expiry without network or writes; refreshability is not verified offline.",
)
@pass_context
def check(ctx: Context, details: bool, offline: bool) -> None:
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

        account = current_account()
        before = inspect_auth_status(account)
        auth_ok = True
        auth_error = None
        auth_exception = None
        if offline:
            auth_ok = before["oauth"]["state"] in {"valid", "refreshable", "offline"}
        else:
            try:
                session = get_web_session()
                browser_api_module.get_current_user(session=session, refresh=True)
            except (SessionExpiredError, ValueError) as e:
                auth_ok = False
                auth_error = str(e)
                auth_exception = e
        status = before if offline else inspect_auth_status(account, error=auth_exception)
        refreshed = before["oauth"]["issued_at"] != status["oauth"]["issued_at"]
        if not auth_ok and before["oauth"]["state"] == "legacy":
            auth_error = legacy_message(account)
        if not offline and status["cas"]["state"] == "present":
            try:
                cached_cas = CasSession.load(account=account)
                cas_ok = cached_cas is not None and cas_ticket_valid(cached_cas, account=account)
            except Exception:
                # CAS is advisory and must never change the OAuth check outcome.
                cas_ok = False
            status["cas"]["state"] = "valid" if cas_ok else "expired"
            if not cas_ok:
                status["cas"]["note"] = f"Run {login_hint(account, cas=True)}."

        # A failed check has to say why without a second run: the reason the
        # login did not go through and the route the request actually took are
        # the two things you need, and asking for `--details` after the fact
        # means re-running against a platform that just refused you.
        verbose = show_details or not auth_ok
        effective_proxy = (
            public_effective_proxy_summary(describe_effective_proxy_config(base_url=cfg.base_url))
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
            "authenticated": None if offline else auth_ok,
            "mode": "offline" if offline else "live",
            **status,
        }
        if not offline:
            result["refreshed"] = refreshed
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
            click.echo("Configuration: OK")
            if offline:
                click.echo("Authentication: not verified (offline)")
            elif auth_ok:
                suffix = " (refreshed now)" if refreshed else ""
                click.echo("Authentication: OK" + suffix)
            else:
                click.echo("Authentication: FAILED")
            for line in format_auth_status(
                status, account, authenticated=None if offline else auth_ok
            ):
                click.echo(line)
            if show_details:
                click.echo(
                    f"Source: {base_url_resolution['source']} (environment overrides account TOML)"
                )
                click.echo(f"Config file: account={'yes' if account_path else 'no'}")
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
            sys.exit(EXIT_API_ERROR if isinstance(auth_exception, TransientAPIError)
                     else EXIT_AUTH_ERROR)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)
