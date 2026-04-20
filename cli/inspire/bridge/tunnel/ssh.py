"""SSH tunnel helpers: connection testing, ProxyCommand, ssh-config generation, and status."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from inspire.platform.web.session.proxy import get_rtunnel_proxy_override

from .config import load_tunnel_config
from .models import BridgeProfile, TunnelConfig, TunnelError
from .rtunnel import _ensure_rtunnel_binary

# ---------------------------------------------------------------------------
# ProxyCommand
# ---------------------------------------------------------------------------


def _get_proxy_command(bridge: BridgeProfile, rtunnel_bin: Path, quiet: bool = False) -> str:
    """Build the ProxyCommand string for SSH.

    Args:
        bridge: Bridge profile with proxy_url
        rtunnel_bin: Path to rtunnel binary
        quiet: If True, suppress rtunnel stderr output (startup/shutdown messages)

    Returns:
        ProxyCommand string for SSH -o option
    """
    import shlex

    # Convert https:// URL to wss:// for websocket
    proxy_url = bridge.proxy_url
    if proxy_url.startswith("https://"):
        ws_url = "wss://" + proxy_url[8:]
    elif proxy_url.startswith("http://"):
        ws_url = "ws://" + proxy_url[7:]
    else:
        ws_url = proxy_url

    def _prepend_proxy_env(command: str) -> str:
        proxy_value = get_rtunnel_proxy_override()
        if not proxy_value:
            return command
        env_prefix = " ".join(
            [
                f"HTTP_PROXY={shlex.quote(proxy_value)}",
                f"HTTPS_PROXY={shlex.quote(proxy_value)}",
                f"http_proxy={shlex.quote(proxy_value)}",
                f"https_proxy={shlex.quote(proxy_value)}",
            ]
        )
        return f"{env_prefix} {command}"

    # ProxyCommand is executed by a shell on the client; quote the URL because it
    # can contain characters like '?' (e.g. token query params) that some shells
    # treat as glob patterns.
    base_cmd = (
        f"{shlex.quote(str(rtunnel_bin))} {shlex.quote(ws_url)} {shlex.quote('stdio://%h:%p')}"
    )
    base_cmd = _prepend_proxy_env(base_cmd)
    if quiet:
        # Wrap in sh -c to redirect stderr, suppressing rtunnel's verbose output
        cmd = f"{base_cmd} 2>/dev/null"
        return f"sh -c {shlex.quote(cmd)}"
    return base_cmd


# ---------------------------------------------------------------------------
# Connection testing
# ---------------------------------------------------------------------------


def _test_ssh_connection(
    bridge: BridgeProfile,
    config: TunnelConfig,
    timeout: int = 10,
) -> bool:
    """Test if SSH connection works via ProxyCommand.

    Args:
        bridge: Bridge profile to test
        config: Tunnel configuration (for rtunnel binary path)
        timeout: SSH connection timeout in seconds (default: 10)

    Returns:
        True if SSH connection succeeds, False otherwise
    """
    # Ensure rtunnel binary exists
    try:
        _ensure_rtunnel_binary(config)
    except TunnelError:
        return False

    proxy_cmd = _get_proxy_command(bridge, config.rtunnel_bin, quiet=True)

    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={timeout}",
                "-o",
                f"ProxyCommand={proxy_cmd}",
                "-o",
                "LogLevel=ERROR",
                "-p",
                str(bridge.ssh_port),
                f"{bridge.ssh_user}@localhost",
                "echo ok",
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
        return result.returncode == 0 and "ok" in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def is_tunnel_available(
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    retries: int = 3,
    retry_pause: float = 2.0,
    progressive: bool = True,
) -> bool:
    """Check if SSH via ProxyCommand is available and responsive.

    Args:
        bridge_name: Name of bridge to check (uses default if None)
        config: Tunnel configuration (loads default if None)
        retries: Number of retries if SSH test fails (default: 3)
        retry_pause: Base pause between retries in seconds (default: 2.0)
        progressive: If True, increase pause with each retry (default: True)

    Returns:
        True if SSH via ProxyCommand works, False otherwise
    """
    if config is None:
        config = load_tunnel_config()

    bridge = config.get_bridge(bridge_name)
    if not bridge:
        return False

    # Test SSH connection with retry
    for attempt in range(retries + 1):
        if _test_ssh_connection(bridge, config):
            return True
        if attempt < retries:
            # Progressive: 2s, 3s, 4s for attempts 0, 1, 2
            pause = retry_pause + (attempt * 1.0) if progressive else retry_pause
            time.sleep(pause)
    return False


# ---------------------------------------------------------------------------
# SSH config generation
# ---------------------------------------------------------------------------


def generate_ssh_config(
    bridge: BridgeProfile,
    rtunnel_path: Path,
    host_alias: Optional[str] = None,
) -> str:
    """Generate SSH config for ProxyCommand mode.

    Args:
        bridge: Bridge profile
        rtunnel_path: Path to rtunnel binary
        host_alias: SSH host alias to use (defaults to bridge name)

    Returns:
        SSH config string to add to ~/.ssh/config
    """
    if host_alias is None:
        host_alias = bridge.name

    # Keep ProxyCommand rendering consistent with runtime SSH command construction
    # so URL/path quoting is always shell-safe in generated ~/.ssh/config entries.
    proxy_cmd = _get_proxy_command(bridge, rtunnel_path, quiet=False)

    ssh_config = f"""Host {host_alias}
    HostName localhost
    User {bridge.ssh_user}
    Port {bridge.ssh_port}
    ProxyCommand {proxy_cmd}
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR"""

    return ssh_config


def generate_all_ssh_configs(config: TunnelConfig) -> str:
    """Generate SSH config for all bridges.

    Args:
        config: Tunnel configuration with all bridges

    Returns:
        SSH config string for all bridges
    """
    if not config.bridges:
        return ""

    rtunnel_path = _ensure_rtunnel_binary(config)
    configs = []
    for bridge in config.list_bridges():
        configs.append(generate_ssh_config(bridge, rtunnel_path))

    return "\n\n".join(configs)


def install_ssh_config(ssh_config: str, host_alias: str) -> dict:
    """Install SSH config to ~/.ssh/config.

    Args:
        ssh_config: SSH config block to add
        host_alias: Host alias to look for (for updating existing entries)

    Returns:
        Dict with keys:
        - success: bool
        - updated: bool (True if existing entry was updated)
        - error: Optional[str]
    """
    ssh_config_path = Path.home() / ".ssh" / "config"
    ssh_config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    existing_content = ""
    if ssh_config_path.exists():
        existing_content = ssh_config_path.read_text()

    # Check if host alias already exists
    # Match "Host <alias>" at start of line, possibly with other hosts on same line
    host_pattern = rf"^Host\s+.*?\b{re.escape(host_alias)}\b.*$"
    match = re.search(host_pattern, existing_content, re.MULTILINE)

    if match:
        # Find the full block to replace (from Host line to next Host line or end)
        block_pattern = rf"(^Host\s+.*?\b{re.escape(host_alias)}\b.*$)((?:\n(?!Host\s).*)*)"
        new_content = re.sub(block_pattern, ssh_config, existing_content, flags=re.MULTILINE)

        ssh_config_path.write_text(new_content)
        return {"success": True, "updated": True, "error": None}
    else:
        # Append new entry
        if existing_content and not existing_content.endswith("\n"):
            existing_content += "\n"
        if existing_content:
            existing_content += "\n"

        ssh_config_path.write_text(existing_content + ssh_config + "\n")
        return {"success": True, "updated": False, "error": None}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def get_tunnel_status(
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
) -> dict:
    """Get tunnel status for a bridge (ProxyCommand mode).

    Args:
        bridge_name: Name of bridge to check (uses default if None)
        config: Tunnel configuration

    Returns:
        Dict with keys:
        - configured: bool (bridge exists)
        - bridge_name: Optional[str]
        - ssh_works: bool
        - proxy_url: Optional[str]
        - rtunnel_path: Optional[str]
        - bridges: list of all bridge names
        - default_bridge: Optional[str]
        - error: Optional[str]
    """
    if config is None:
        config = load_tunnel_config()

    bridge = config.get_bridge(bridge_name)

    status = {
        "configured": bridge is not None,
        "bridge_name": bridge.name if bridge else None,
        "ssh_works": False,
        "proxy_url": bridge.proxy_url if bridge else None,
        "rtunnel_path": str(config.rtunnel_bin) if config.rtunnel_bin.exists() else None,
        "bridges": [b.name for b in config.list_bridges()],
        "default_bridge": config.default_bridge,
        "error": None,
    }

    if not bridge:
        if bridge_name:
            status["error"] = f"Bridge '{bridge_name}' not found."
        else:
            status["error"] = "No bridge configured. Run 'inspire tunnel add <name> <url>' first."
        return status

    # Check if rtunnel binary exists
    if not config.rtunnel_bin.exists():
        try:
            _ensure_rtunnel_binary(config)
            status["rtunnel_path"] = str(config.rtunnel_bin)
        except TunnelError as e:
            status["error"] = str(e)
            return status

    # Test SSH connection
    status["ssh_works"] = _test_ssh_connection(bridge, config)
    if not status["ssh_works"]:
        status["error"] = "SSH connection failed. Check proxy URL and Bridge rtunnel server."

    return status
