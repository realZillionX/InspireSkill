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
import click.exceptions as click_exceptions

from inspire import __version__
from inspire.cli.logging_setup import clear_debug_logging, configure_debug_logging
from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
)
from inspire.cli.commands import (
    account,
    cache,
    job,
    resources,
    dataset,
    notebook,
    init,
    image,
    project,
    hpc,
    model,
    ray,
    serving,
    tensorboard,
    uninstall,
    update,
)
from inspire.cli.commands.serving.serving_commands import (
    events_serving,
    instances_serving,
    start_serving,
)
from inspire.cli.utils.update_notice import maybe_notify_update, maybe_spawn_check
from inspire.cli.utils.output_guard import (
    clear_parser_redactions,
    parser_echo,
    set_parser_redactions,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.env_bootstrap import bootstrap_env_file


_PARSER_GUARD_INSTALLED = False


def _install_pre_parse_output_guard(args: list[str] | tuple[str, ...]) -> None:
    """Install the parser-diagnostic redactions before Click renders errors."""
    global _PARSER_GUARD_INSTALLED
    set_parser_redactions(args)
    if _PARSER_GUARD_INSTALLED:
        return
    # Click's exception module keeps its own imported echo reference, so the
    # callback-time monkey patch alone cannot sanitize parser diagnostics.
    click_exceptions.echo = parser_echo
    _PARSER_GUARD_INSTALLED = True


class _NameOnlyGroup(click.Group):
    def main(self, *args, **kwargs):  # noqa: ANN002, ANN003
        cli_args = kwargs.get("args")
        if cli_args is None and args:
            cli_args = args[0]
        if cli_args is None:
            cli_args = sys.argv[1:]
        _install_pre_parse_output_guard(tuple(str(value) for value in cli_args))
        try:
            return super().main(*args, **kwargs)
        finally:
            clear_parser_redactions()


@click.group(cls=_NameOnlyGroup)
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
    help="Write diagnostic details to the debug log.",
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
    """Manage Inspire resources by name.

    \b
    Normal workflow:
        1. `inspire account context` lists usable resource names.
        2. `inspire <kind> quota --workspace <name>` lists valid quotas.
        3. `inspire <kind> create ...` creates a workload.
        4. `status`, `events`, `logs`, and `metrics` inspect it.

    \b
    Output:
        --json prints structured script output.

    \b
    Examples:
        inspire job create --name "pr-123" --workspace 分布式训练空间 \
          --project <project> --group H200-2号机房 --quota "4,80,800" \
          --command "bash train.sh"
        inspire job status pr-123 --workspace 分布式训练空间
        inspire notebook list --workspace 分布式训练空间
        inspire resources availability --workspace 分布式训练空间
    """
    ctx.json_output = json_output
    ctx.debug = debug

    try:
        bootstrap_env_file(env_file=env_file, disabled=no_env_file)
    except click.ClickException as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)

    if debug:
        ctx.debug_report_path = configure_debug_logging(argv=sys.argv)
    else:
        clear_debug_logging()

    # Keep the background update check detached and silent. The upgrade notice
    # itself fires for everyone (stderr only) but is never allowed to
    # contaminate JSON output.
    # `uninstall` is excluded for a different reason than `update`: the check
    # writes ~/.inspire/update-status.json, which the uninstall is on its way
    # to delete, and a detached child would outlive the venv it runs from.
    if not (len(sys.argv) > 1 and sys.argv[1] in {"update", "uninstall"}):
        try:
            if not json_output:
                maybe_notify_update()
            maybe_spawn_check()
        except Exception:
            pass

    if not (
        len(sys.argv) > 1
        and sys.argv[1] in {"account", "cache", "update", "uninstall"}
    ):
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
@click.option("--expected-version", required=True, help="Expected installed version.")
@click.option("--cli-only", is_flag=True, help="Skip skill refresh.")
@click.option("--silent", is_flag=True, help="Suppress post-update output.")
@click.option(
    "--previous-version",
    default=None,
    hidden=True,
    help="Accepted for compatibility with pre-6.3 callers; ignored.",
)
def post_update(
    expected_version: str,
    cli_only: bool,
    silent: bool,
    previous_version: str | None,
) -> None:
    """Internal hook run from the newly installed CLI after self-update.

    ``--previous-version`` is dead weight here, but CLIs at or below v6.2.0
    always pass it when they hand off to the version they just installed.
    Dropping the option would make every upgrade from a released build fail
    at the handoff with ``No such option``, so it stays accepted (and
    ignored) until those versions are no longer in the field.
    """
    del previous_version
    from inspire.cli.commands.update import _run_post_update_tasks

    if not _run_post_update_tasks(
        expected_version=expected_version,
        cli_only=cli_only,
        silent=silent,
    ):
        raise SystemExit(1)


# Register command groups
serving.add_command(start_serving)
serving.add_command(events_serving)
serving.add_command(instances_serving)
main.add_command(account)
main.add_command(cache)
main.add_command(job)
main.add_command(resources)
main.add_command(dataset)
main.add_command(notebook)
main.add_command(init)
main.add_command(image)
main.add_command(project)
main.add_command(hpc)
main.add_command(model)
main.add_command(ray)
main.add_command(serving)
main.add_command(tensorboard)
main.add_command(uninstall)
main.add_command(update)
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
        from inspire.cli.formatters import human_formatter, json_formatter

        public_message = json_formatter.sanitize_text(
            str(e) or type(e).__name__,
            redact_paths=True,
            redact_urls=True,
            redact_platform_paths=True,
        )
        if "--json" in sys.argv[1:]:
            click.echo(
                json_formatter.format_json_error(
                    "Error",
                    public_message,
                    EXIT_GENERAL_ERROR,
                ),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(public_message), err=True)
        sys.exit(EXIT_GENERAL_ERROR)


if __name__ == "__main__":  # pragma: no cover
    cli()
