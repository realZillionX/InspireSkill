"""SSH-related notebook command group."""

from __future__ import annotations

import shlex

import click

from inspire.cli.context import Context, pass_context

from .notebook_ssh_flow import run_notebook_ssh


_LEGACY_SSH_SUBCOMMANDS = {
    "connect": "Use `inspire notebook ssh <name> --workspace <workspace>` to open SSH, "
    "or `inspire notebook connection refresh <name> --workspace <workspace>` to "
    "refresh the cache.",
    "test": "Use `inspire notebook connection status <name>`.",
    "refresh": "Use `inspire notebook connection refresh <name> --workspace <workspace>`.",
    "forget": "Use `inspire notebook connection forget <name>`.",
}


class NotebookSSHGroup(click.Group):
    """Click group that treats unknown first tokens as notebook names."""

    def resolve_command(self, ctx: click.Context, args: list[str]):
        if args and args[0] in _LEGACY_SSH_SUBCOMMANDS:
            hint = _LEGACY_SSH_SUBCOMMANDS[args[0]]
            raise click.UsageError(
                f"`inspire notebook ssh {args[0]}` has been removed. {hint} "
                f"If a notebook is literally named '{args[0]}', run "
                f"`inspire notebook ssh open {args[0]}`."
            )
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            return "open", self.commands["open"], args
        return super().resolve_command(ctx, args)


@click.command(
    "open",
    hidden=True,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("notebook")
@click.argument("command_parts", nargs=-1, type=click.UNPROCESSED)
@click.option("--workspace", required=False, help="Workspace name.")
@click.option("--account", required=False, help="Account name for this notebook target.")
@click.option(
    "--ignore-target-cache",
    is_flag=True,
    help="Ignore the remembered notebook target and resolve candidates again.",
)
@click.option("--wait/--no-wait", default=True, help="Wait for notebook to reach RUNNING status")
@click.option(
    "--pubkey",
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help="SSH public key path to authorize",
)
@click.option(
    "--port",
    type=click.IntRange(1, 65535),
    default=31337,
    show_default=True,
    help="Advanced: connection service port inside notebook",
)
@click.option(
    "--ssh-port",
    type=click.IntRange(1, 65535),
    default=22222,
    show_default=True,
    help="Advanced: SSH service port inside notebook",
)
@click.option(
    "--command-timeout",
    type=click.IntRange(0),
    default=None,
    help="Timeout in seconds for remote command execution (default: 300, 0 disables)",
)
@click.option("--debug-playwright", is_flag=True, help="Run browser automation visibly")
@click.option(
    "--timeout",
    "setup_timeout",
    type=click.IntRange(1),
    default=300,
    show_default=True,
    help="Timeout in seconds for notebook connection setup",
)
@pass_context
def _ssh_open(
    ctx: Context,
    notebook: str,
    command_parts: tuple[str, ...],
    workspace: str | None,
    account: str | None,
    ignore_target_cache: bool,
    wait: bool,
    pubkey: str | None,
    port: int,
    ssh_port: int,
    command_timeout: int | None,
    debug_playwright: bool,
    setup_timeout: int,
) -> None:
    """Open an SSH shell or run a command on a notebook."""
    command = shlex.join(command_parts) if command_parts else None
    run_notebook_ssh(
        ctx,
        notebook_id=notebook,
        workspace=workspace,
        wait=wait,
        pubkey=pubkey,
        port=port,
        ssh_port=ssh_port,
        command=command,
        command_timeout=command_timeout,
        debug_playwright=debug_playwright,
        setup_timeout=setup_timeout,
        account=account,
        ignore_target_cache=ignore_target_cache,
    )


@click.group("ssh", cls=NotebookSSHGroup)
def notebook_ssh() -> None:
    """Open SSH to a notebook or run a remote command.

    Use `inspire notebook ssh <notebook>` for an interactive shell, or
    `inspire notebook ssh <notebook> -- <command>` for a one-shot command.
    Cached connection management is available through the
    `inspire notebook connection` command group.
    """


notebook_ssh.add_command(_ssh_open, name="open")


__all__ = ["notebook_ssh"]
