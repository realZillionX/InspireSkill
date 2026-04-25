"""`notebook connections` -- list cached SSH connections by notebook name."""

from __future__ import annotations

import concurrent.futures

import click

from inspire.bridge.tunnel import load_tunnel_config
from inspire.bridge.tunnel.ssh import _test_ssh_connection
from inspire.cli.context import Context, pass_context
from inspire.cli.formatters import human_formatter, json_formatter


def _check_bridges(bridges, config, timeout=5):
    """Test SSH connectivity for all cached notebooks in parallel.

    Returns a dict mapping notebook name to bool (True = connected).
    """
    results: dict[str, bool] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(bridges)) as pool:
        futures = {pool.submit(_test_ssh_connection, b, config, timeout): b.name for b in bridges}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception:
                results[name] = False
    return results


def _sort_bridges_for_display(bridges, *, ssh_status: dict[str, bool], no_check: bool):
    """Sort cached notebooks for output.

    When live checks are enabled, connected entries are listed first, then the
    remaining entries alphabetically by notebook name.
    """
    if no_check:
        return sorted(bridges, key=lambda b: b.name)
    return sorted(
        bridges,
        key=lambda b: (
            0 if ssh_status.get(b.name, False) else 1,
            b.name,
        ),
    )


@click.command("list")
@click.option(
    "--no-check",
    is_flag=True,
    help="Skip live SSH connectivity check (faster output).",
)
@pass_context
def tunnel_list(ctx: Context, no_check: bool) -> None:
    """List cached SSH connections by notebook name.

    \b
    Example:
        inspire notebook connections
        inspire notebook connections --no-check
    """
    config = load_tunnel_config()

    bridges = config.list_bridges()

    if not bridges:
        if ctx.json_output:
            click.echo(json_formatter.format_json({"bridges": [], "default": None}))
        else:
            click.echo("No cached notebook connections.")
            click.echo("")
            click.echo("Bootstrap one with: inspire notebook ssh <notebook>")
        return

    # Check SSH connectivity unless --no-check
    ssh_status: dict[str, bool] = {}
    if not no_check:
        ssh_status = _check_bridges(bridges, config)

    ordered_bridges = _sort_bridges_for_display(bridges, ssh_status=ssh_status, no_check=no_check)

    if ctx.json_output:
        bridge_dicts = []
        for b in ordered_bridges:
            d = b.to_dict()
            if not no_check:
                d["ssh_works"] = ssh_status.get(b.name, False)
            bridge_dicts.append(d)
        click.echo(
            json_formatter.format_json(
                {
                    "bridges": bridge_dicts,
                    "default": config.default_bridge,
                }
            )
        )
        return

    click.echo("Cached notebook connections:")
    click.echo("=" * 50)
    for bridge in ordered_bridges:
        is_default = bridge.name == config.default_bridge
        default_mark = "* " if is_default else "  "
        no_internet_mark = " [no internet]" if not bridge.has_internet else ""

        status_mark = ""
        if not no_check:
            if ssh_status.get(bridge.name, False):
                status_mark = " " + click.style("[connected]", fg="green")
            else:
                status_mark = " " + click.style("[not responding]", fg="red")

        click.echo(f"{default_mark}{bridge.name}:{no_internet_mark}{status_mark}")
        click.echo(f"    URL: {bridge.proxy_url}")
        click.echo(f"    SSH: {bridge.ssh_user}@localhost:{bridge.ssh_port}")
        click.echo(f"    Internet: {'yes' if bridge.has_internet else 'no'}")
        if is_default:
            click.echo(human_formatter.format_success("    (default)"))
    click.echo("")
    click.echo("* = default cached notebook")
