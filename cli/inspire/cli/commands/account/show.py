"""Account show command – account-scope settings and effective proxy routing."""

from __future__ import annotations

from typing import Any

import click

from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.platform.web.session.proxy import describe_effective_proxy_config

from .proxy_output import (
    format_effective_proxy_lines,
    public_effective_proxy_summary,
)
from .settings_view import (
    echo_groups,
    echo_source_legend,
    json_values,
    matching_categories,
    select_groups,
)


@click.command("show")
@click.option(
    "--filter",
    "-F",
    "filter_category",
    metavar="CATEGORY",
    help="Filter by category (e.g., 'Proxy', 'API').",
)
@click.option(
    "--details",
    is_flag=True,
    help="Include defaults, value sources, descriptions, and config-file presence.",
)
@pass_context
def show(ctx: Context, filter_category: str | None, details: bool) -> None:
    """Show the active account's settings and effective proxy routing.

    Identity, API, and proxy values are reported as set or not set, never
    printed. The effective proxy section is the one thing no config file
    holds: it merges the account's `[proxy]` block with the shell's
    `http_proxy` / `NO_PROXY` variables and reports the route each of the
    three client stacks ends up taking.

    \b
    Examples:
        inspire account show
        inspire account show --details
        inspire account show --filter Proxy
        inspire --json account show
    """
    from inspire.accounts import current_account

    try:
        cfg, sources = Config.from_files_and_env(require_credentials=False)
        if filter_category and not matching_categories(filter_category):
            _handle_error(
                ctx,
                "ValidationError",
                f"No category matching '{filter_category}'.",
                EXIT_VALIDATION_ERROR,
                hint="Categories: Authentication, API, Proxy, Tunnel.",
            )
            return

        account_path, _project_path = Config.get_config_paths()
        active_account = scrub_raw_ids(current_account() or "") or None
        groups = select_groups(
            cfg,
            sources,
            filter_category=filter_category,
            details=details,
        )
        effective_proxy = public_effective_proxy_summary(
            describe_effective_proxy_config(base_url=cfg.base_url)
        )

        if ctx.json_output:
            result: dict[str, Any] = {
                "account": active_account,
                "values": json_values(cfg, sources, groups, details=details),
                "effective_proxy": effective_proxy,
            }
            if details:
                # Only presence, never the path: `config_file` is on the JSON
                # formatter's engineering-key deny list and would be dropped.
                result["account_config_present"] = bool(account_path)
                result["prefer_source"] = getattr(cfg, "prefer_source", "env")
            click.echo(json_formatter.format_json(result))
            return

        click.echo(click.style(f"Account: {active_account or '-'}", bold=True))
        if details:
            click.echo(f"Config file: {'yes' if account_path else 'no'}")
        click.echo()

        echo_groups(cfg, sources, groups, details=details)

        # The effective-proxy block is the reason this command exists, so it
        # survives an empty option table: "nothing is set in the account file"
        # and "requests still go through a shell proxy" are both true at once.
        for line in format_effective_proxy_lines(effective_proxy):
            click.echo(line)

        if details:
            click.echo()
            echo_source_legend()

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)
