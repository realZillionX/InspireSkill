"""Tunnel test command."""

from __future__ import annotations

import sys

import click

from inspire.bridge.tunnel import TunnelNotAvailableError, load_tunnel_config, run_ssh_command
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, pass_context
from inspire.cli.formatters import human_formatter, json_formatter


@click.command("test")
@click.argument("notebook", required=False)
@pass_context
def tunnel_test(ctx: Context, notebook: str) -> None:
    """Test SSH connection to a cached notebook and show timing.

    NOTEBOOK is the cached notebook name (omit to use the default).

    \b
    Examples:
        inspire notebook test
        inspire notebook test my-notebook
    """
    import time

    bridge = notebook
    config = load_tunnel_config()
    bridge_profile = config.get_bridge(bridge)

    if not bridge_profile:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "ConfigError",
                    "No cached notebook connection",
                    EXIT_CONFIG_ERROR,
                    hint="Bootstrap one with: inspire notebook ssh <notebook>",
                ),
                err=True,
            )
        else:
            click.echo(
                human_formatter.format_error(
                    "No cached notebook connection. Bootstrap one with: "
                    "inspire notebook ssh <notebook>"
                ),
                err=True,
            )
        sys.exit(EXIT_CONFIG_ERROR)

    try:
        start = time.time()
        result = run_ssh_command(
            "hostname", bridge_name=bridge_profile.name, config=config, timeout=30
        )
        elapsed = time.time() - start

        hostname = result.stdout.strip()

        if ctx.json_output:
            if result.returncode == 0:
                click.echo(
                    json_formatter.format_json(
                        {
                            "notebook": bridge_profile.name,
                            "hostname": hostname,
                            "elapsed_ms": int(elapsed * 1000),
                        }
                    )
                )
            else:
                click.echo(
                    json_formatter.format_json_error(
                        "TunnelError",
                        f"Connection failed: {result.stderr}",
                        EXIT_GENERAL_ERROR,
                    ),
                    err=True,
                )
                sys.exit(EXIT_GENERAL_ERROR)
        else:
            if result.returncode == 0:
                click.echo(
                    human_formatter.format_success(
                        f"Notebook '{bridge_profile.name}': Connected to {hostname}"
                    )
                )
                click.echo(f"Response time: {elapsed:.2f}s")
            else:
                click.echo(human_formatter.format_error(f"Connection failed: {result.stderr}"))
                sys.exit(EXIT_GENERAL_ERROR)

    except TunnelNotAvailableError as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("TunnelError", str(e), EXIT_GENERAL_ERROR),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(str(e)), err=True)
        sys.exit(EXIT_GENERAL_ERROR)
    except Exception as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("Error", str(e), EXIT_GENERAL_ERROR),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(f"Connection failed: {e}"), err=True)
        sys.exit(EXIT_GENERAL_ERROR)
