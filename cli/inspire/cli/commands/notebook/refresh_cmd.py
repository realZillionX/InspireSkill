"""Tunnel update command."""

from __future__ import annotations

import sys

import click

from inspire.bridge.tunnel import load_tunnel_config, save_tunnel_config
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import human_formatter, json_formatter


@click.command("update")
@click.argument("name")
@click.option("--url", help="Update the proxy URL")
@click.option("--ssh-user", help="Update the SSH user")
@click.option("--ssh-port", type=int, help="Update the SSH port")
@click.option(
    "--has-internet",
    is_flag=True,
    flag_value=True,
    default=None,
    help="Mark bridge as having internet access",
)
@click.option(
    "--no-internet",
    is_flag=True,
    flag_value=True,
    default=None,
    help="Mark bridge as having no internet access",
)
@pass_context
def tunnel_update(
    ctx: Context,
    name: str,
    url: str,
    ssh_user: str,
    ssh_port: int,
    has_internet: bool,
    no_internet: bool,
) -> None:
    """Update an existing saved notebook alias.

    \b
    Examples:
        inspire notebook refresh mybridge --has-internet
        inspire notebook refresh mybridge --no-internet
        inspire notebook refresh mybridge --url "https://new-url.../proxy/31337/"
        inspire notebook refresh mybridge --ssh-port 22223
    """
    config = load_tunnel_config()

    if name not in config.bridges:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "NotFound",
                    f"Bridge '{name}' not found",
                    EXIT_CONFIG_ERROR,
                ),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(f"Bridge '{name}' not found"), err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    if has_internet and no_internet:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error(
                    "ValidationError",
                    "Cannot specify both --has-internet and --no-internet",
                    EXIT_CONFIG_ERROR,
                ),
                err=True,
            )
        else:
            click.echo(
                human_formatter.format_error(
                    "Cannot specify both --has-internet and --no-internet"
                ),
                err=True,
            )
        sys.exit(EXIT_CONFIG_ERROR)

    bridge = config.bridges[name]
    updated_fields: list[str] = []

    if url is not None:
        bridge.proxy_url = url
        updated_fields.append("url")
    if ssh_user is not None:
        bridge.ssh_user = ssh_user
        updated_fields.append("ssh_user")
    if ssh_port is not None:
        bridge.ssh_port = ssh_port
        updated_fields.append("ssh_port")
    if has_internet:
        bridge.has_internet = True
        updated_fields.append("has_internet")
    elif no_internet:
        bridge.has_internet = False
        updated_fields.append("has_internet")

    if not updated_fields:
        message = (
            "No fields to update. Use --url, --ssh-user, --ssh-port, --has-internet, "
            "or --no-internet."
        )
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("ValidationError", message, EXIT_CONFIG_ERROR),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(message), err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    save_tunnel_config(config)

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "status": "updated",
                    "name": name,
                    "updated_fields": updated_fields,
                    "bridge": bridge.to_dict(),
                }
            )
        )
        return

    click.echo(f"Updated bridge: {name}")
    for field in updated_fields:
        if field == "url":
            click.echo(f"  URL: {bridge.proxy_url}")
        elif field == "ssh_user":
            click.echo(f"  SSH user: {bridge.ssh_user}")
        elif field == "ssh_port":
            click.echo(f"  SSH port: {bridge.ssh_port}")
        elif field == "has_internet":
            click.echo(f"  Internet: {'yes' if bridge.has_internet else 'no'}")
