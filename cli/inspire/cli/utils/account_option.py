"""The shared account selector for every CLI command."""

from __future__ import annotations

import click

from inspire.accounts import account_exists, account_scope
from inspire.cli.context import Context, EXIT_CONFIG_ERROR
from inspire.cli.utils.errors import exit_with_error


def _select_account(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    del param
    if ctx.resilient_parsing:
        return value
    if value is not None:
        value = value.strip()
        if not account_exists(value):
            output = ctx.find_object(Context) or Context()
            output.json_output = output.json_output or bool(ctx.params.get("json_output"))
            exit_with_error(
                output, "ConfigError", f"Account not found: {value}", EXIT_CONFIG_ERROR,
                hint="Pass one configured account alias; see `inspire account list`.",
            )
        ctx.with_resource(account_scope(value))
    return value


def install_account_options(command: click.Command) -> None:
    """Use the same selector at root, group, and subcommand positions.

    Existing Notebook options retain their callback argument for internal
    callers. Every other command consumes the selector through account_scope.
    """
    for param in command.params:
        if isinstance(param, click.Option) and "--account" in param.opts:
            param.callback = _select_account
            param.help = "Use this account for this command; keep the saved default unchanged."
            break
    else:
        command.params.append(click.Option(
            ["--account"], metavar="NAME", expose_value=False,
            callback=_select_account,
            help="Use this account for this command; keep the saved default unchanged.",
        ))
    if isinstance(command, click.Group):
        for child in command.commands.values():
            install_account_options(child)
