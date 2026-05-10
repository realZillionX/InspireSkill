"""Inspire CLI - Main entry point.

Usage:
    inspire job create --name "pr-123" --quota "4,80,800" --command "bash train.sh"
    inspire job status <name>
    inspire notebook list
    inspire resources list
"""

import logging
import sys
import click

from inspire import __version__
from inspire.cli.logging_setup import clear_debug_logging, configure_debug_logging
from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_GENERAL_ERROR,
)
from inspire.cli.commands import (
    account,
    job,
    resources,
    config,
    notebook,
    init,
    image,
    project,
    hpc,
    model,
    ray,
    serving,
    update,
    user,
)
from inspire.cli.utils.update_notice import maybe_notify_update, maybe_spawn_check


@click.group()
@click.version_option(version=__version__, prog_name="inspire")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Output as JSON for scripts or structured automation.",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug logging",
)
@pass_context
def main(ctx: Context, json_output: bool, debug: bool) -> None:
    """Inspire Training Platform CLI.

    Interact with the Inspire HPC platform to manage notebooks, submit and
    monitor training / HPC / Ray jobs, and inspect compute resources.

    \b
    Output:
        Default output is name-first.
        Default human output is the interactive observation surface.
        JSON is for scripts and structured automation.
        Use JSON only for scripts or structured automation.

    \b
    Global options:
        --json prints structured script output.

    \b
    Examples:
        inspire job create --name "pr-123" --quota "4,80,800" --command "bash train.sh"
        inspire job status pr-123
        inspire notebook list
        inspire resources list
    """
    ctx.json_output = json_output
    ctx.debug = debug

    if debug:
        ctx.debug_report_path = configure_debug_logging(argv=sys.argv)
    else:
        clear_debug_logging()

    # Opportunistic update check: prints a one-line notice to stderr if the
    # on-disk cache says a newer version exists, and fires a detached
    # background check when the cache is stale. Never raises, never blocks.
    # Skipped for `inspire update ...` (handled inside that command itself)
    # and when INSPIRE_SKIP_UPDATE_CHECK=1.
    if not (len(sys.argv) > 1 and sys.argv[1] == "update"):
        try:
            maybe_notify_update()
            maybe_spawn_check()
        except Exception:
            pass


# Register command groups
main.add_command(account)
main.add_command(job)
main.add_command(resources)
main.add_command(config)
main.add_command(notebook)
main.add_command(init)
main.add_command(image)
main.add_command(project)
main.add_command(hpc)
main.add_command(model)
main.add_command(ray)
main.add_command(serving)
main.add_command(update)
main.add_command(user)


def cli() -> None:
    """Entry point for the CLI."""
    try:
        main()
    except Exception as e:  # pragma: no cover - top-level safety net
        # Final firewall: format the message via the same formatter every
        # other command uses, so the user never sees a `Traceback (most
        # recent call last):` from a path that forgot to wrap its own
        # exceptions. The full traceback still lands in the debug log
        # (configured by `--debug`), which is where it belongs.
        logging.getLogger(__name__).exception("Unhandled exception in inspire CLI")
        from inspire.cli.formatters import human_formatter

        click.echo(human_formatter.format_error(str(e) or type(e).__name__), err=True)
        sys.exit(EXIT_GENERAL_ERROR)


if __name__ == "__main__":  # pragma: no cover
    cli()
