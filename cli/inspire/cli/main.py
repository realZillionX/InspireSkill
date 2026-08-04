"""Inspire CLI - Main entry point.

Usage:
    inspire job create --name "pr-123" --workspace <workspace> --project <project> \
        --group <full-group-name> --quota "4,80,800" --command "bash train.sh"
    inspire job status <name> --workspace <workspace>
    inspire notebook list --workspace <workspace>
    inspire resources availability --workspace <workspace>
"""

import logging
import sys
from pathlib import Path

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
    cache,
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
from inspire.cli.utils.output_guard import install_output_guard
from inspire.cli.env_bootstrap import bootstrap_env_file


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
@click.option(
    "--env-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Load variables from this dotenv file before running.",
)
@click.option(
    "--no-env-file",
    is_flag=True,
    help="Do not load the project-declared dotenv file.",
)
@pass_context
def main(
    ctx: Context,
    json_output: bool,
    debug: bool,
    env_file: Path | None,
    no_env_file: bool,
) -> None:
    """Inspire Training Platform CLI.

    Use Inspire from the local terminal: configure accounts, inspect live
    resources, create notebooks, submit GPU jobs / CPU HPC / Ray workloads,
    manage images and models, deploy servings, and observe events, logs,
    metrics, and status.

    \b
    Normal workflow:
        1. `inspire config context` lists usable names for workspaces,
           projects, and compute groups.
        2. `inspire <kind> quota --workspace <name|all>` shows valid
           `--quota gpu,cpu,mem` triples for the workload family.
        3. `inspire <kind> create ...` submits the workload using visible
           names, or `inspire <kind> profile set ...` stores reusable
           workspace/project/group/quota/image conditions.
        4. `events`, `logs`, `metrics`, `status`, and `instances` diagnose
           scheduling, startup, runtime progress, and cleanup decisions.

    \b
    Output:
        Default output is name-first.
        Default human output is the interactive observation surface.
        JSON output is for scripts and structured automation.

    \b
    Global options:
        --json prints structured script output.

    \b
    Examples:
        inspire job create --name "pr-123" --workspace 分布式训练空间 \
          --project CI-情境智能 --group H200-2号机房 --quota "4,80,800" \
          --command "bash train.sh"
        inspire job status pr-123 --workspace 分布式训练空间
        inspire notebook list --workspace 分布式训练空间
        inspire resources availability --workspace 分布式训练空间
    """
    ctx.json_output = json_output
    ctx.debug = debug
    install_output_guard()

    bootstrap_env_file(env_file=env_file, disabled=no_env_file)

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

    if not (len(sys.argv) > 1 and sys.argv[1] in {"account", "cache", "update"}):
        try:
            from inspire.cli.utils.resource_index_refresh import (
                maybe_spawn_periodic_refresh,
            )

            maybe_spawn_periodic_refresh()
        except Exception:
            pass


@click.command("_ensure-playwright-runtime", hidden=True)
@click.option("--silent", is_flag=True, help="Suppress runtime setup output.")
def ensure_playwright_runtime(silent: bool) -> None:
    """Internal installer/update hook for browser runtime setup."""
    from inspire.cli.commands.update import _ensure_playwright_runtime

    if not _ensure_playwright_runtime(silent=silent):
        raise SystemExit(1)


@click.command("_post-update", hidden=True)
@click.option("--previous-version", required=True, help="Version before the outer update.")
@click.option("--expected-version", required=True, help="Expected installed version.")
@click.option("--cli-only", is_flag=True, help="Skip skill refresh.")
@click.option("--silent", is_flag=True, help="Suppress post-update output.")
def post_update(
    previous_version: str,
    expected_version: str,
    cli_only: bool,
    silent: bool,
) -> None:
    """Internal hook run from the newly installed CLI after self-update."""
    from inspire.cli.commands.update import _run_post_update_tasks

    if not _run_post_update_tasks(
        expected_version=expected_version,
        previous_version=previous_version,
        cli_only=cli_only,
        silent=silent,
    ):
        raise SystemExit(1)


# Register command groups
main.add_command(account)
main.add_command(cache)
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
main.add_command(ensure_playwright_runtime)
main.add_command(post_update)


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
