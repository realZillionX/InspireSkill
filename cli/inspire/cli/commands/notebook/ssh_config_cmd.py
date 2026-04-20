"""Tunnel ssh-config command."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import click

from inspire.bridge.tunnel import (
    TunnelError,
    generate_all_ssh_configs,
    generate_ssh_config,
    get_rtunnel_path,
    install_ssh_config,
    load_tunnel_config,
)
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, pass_context
from inspire.cli.formatters import human_formatter, json_formatter


@click.command("ssh-config")
@click.option("--alias", "-a", "bridge", help="Generate config for a specific alias only")
@click.option("--bridge", "-b", "bridge", hidden=True, help="(Deprecated) same as --alias")
@click.option("--install", is_flag=True, help="Automatically append to ~/.ssh/config")
@pass_context
def tunnel_ssh_config(ctx: Context, bridge: str, install: bool) -> None:
    """Generate SSH config for direct SSH access to all bridges.

    This allows using 'ssh <bridge-name>', 'scp', 'rsync', etc.
    directly without going through the inspire command.

    \b
    Benefits:
        - Works with scp, rsync, git, and all SSH-based tools
        - Each connection gets a fresh tunnel
        - No background process to manage

    \b
    Examples:
        inspire notebook ssh-config                    # Show all bridges config
        inspire notebook ssh-config --install          # Auto-add to ~/.ssh/config
        inspire notebook ssh-config -b mybridge       # Show specific bridge only

    \b
    After setup, use:
        ssh <bridge-name>
        scp file.txt <bridge-name>:/path/
        rsync -av ./local/ <bridge-name>:/remote/
    """
    try:
        config = load_tunnel_config()

        if not config.bridges:
            click.echo(
                human_formatter.format_error(
                    "No bridges configured. Run 'inspire notebook ssh <id> --save-as <name> <URL>' first."
                ),
                err=True,
            )
            sys.exit(EXIT_CONFIG_ERROR)

        rtunnel_path = get_rtunnel_path(config)

        if bridge:
            bridge_profile = config.get_bridge(bridge)
            if not bridge_profile:
                click.echo(human_formatter.format_error(f"Bridge '{bridge}' not found"), err=True)
                sys.exit(EXIT_CONFIG_ERROR)

            ssh_config = generate_ssh_config(bridge_profile, rtunnel_path, host_alias=bridge)

            if ctx.json_output:
                click.echo(
                    json_formatter.format_json(
                        {
                            "bridge": bridge,
                            "config": ssh_config,
                            "rtunnel_path": str(rtunnel_path),
                        }
                    )
                )
                return

            if install:
                result = install_ssh_config(ssh_config, bridge)
                if result["updated"]:
                    click.echo(
                        human_formatter.format_success(f"Updated '{bridge}' entry in ~/.ssh/config")
                    )
                else:
                    click.echo(human_formatter.format_success(f"Added '{bridge}' to ~/.ssh/config"))
                click.echo("")
                click.echo("You can now use:")
                click.echo(f"  ssh {bridge}")
            else:
                click.echo(f"SSH config for bridge '{bridge}':\n")
                click.echo("-" * 50)
                click.echo(ssh_config)
                click.echo("-" * 50)
            return

        all_configs = generate_all_ssh_configs(config)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "bridges": list(config.bridges.keys()),
                        "config": all_configs,
                        "rtunnel_path": str(rtunnel_path),
                    }
                )
            )
            return

        if install:
            ssh_config_path = Path.home() / ".ssh" / "config"
            ssh_config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

            if ssh_config_path.exists():
                content = ssh_config_path.read_text()
                for bridge_name in list(config.bridges.keys()):
                    pattern = rf"Host\s+.*?\b{re.escape(bridge_name)}\b.*?(?=\nHost\s|\Z)"
                    content = re.sub(pattern, "", content, flags=re.DOTALL | re.MULTILINE)
                ssh_config_path.write_text(content)

            with ssh_config_path.open("a", encoding="utf-8") as f:
                f.write("\n")
                f.write("# Inspire Bridges (auto-generated)\n")
                f.write(all_configs)
                f.write("\n")

            click.echo(
                human_formatter.format_success(
                    f"Added {len(config.bridges)} bridge(s) to ~/.ssh/config"
                )
            )
            click.echo("")
            click.echo("You can now use:")
            for b in sorted(config.bridges.keys()):
                click.echo(f"  ssh {b}")
        else:
            click.echo("SSH config for all bridges:\n")
            click.echo("-" * 50)
            click.echo(all_configs)
            click.echo("-" * 50)
            click.echo("")
            click.echo("Or run with --install to auto-add:")
            click.echo("  inspire notebook ssh-config --install")

    except TunnelError as e:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json_error("TunnelError", str(e), EXIT_GENERAL_ERROR),
                err=True,
            )
        else:
            click.echo(human_formatter.format_error(str(e)), err=True)
        sys.exit(EXIT_GENERAL_ERROR)
