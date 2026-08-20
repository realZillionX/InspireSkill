"""SSH tunnel helpers: connection testing, ProxyCommand, and status."""

from __future__ import annotations

import os
import sys
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


def _ws_proxy_url(proxy_url: str) -> str:
    if proxy_url.startswith("https://"):
        return "wss://" + proxy_url[8:]
    if proxy_url.startswith("http://"):
        return "ws://" + proxy_url[7:]
    return proxy_url


def _proxy_env() -> dict[str, str]:
    proxy_value = get_rtunnel_proxy_override()
    if not proxy_value:
        return {}
    return {
        "HTTP_PROXY": proxy_value,
        "HTTPS_PROXY": proxy_value,
        "http_proxy": proxy_value,
        "https_proxy": proxy_value,
    }


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

    ws_url = _ws_proxy_url(bridge.proxy_url)

    def _prepend_proxy_env(command: str) -> str:
        env_values = _proxy_env()
        if not env_values:
            return command
        env_prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in env_values.items())
        return f"{env_prefix} {command}"

    if sys.platform == "win32":
        # Win32-OpenSSH does not run ProxyCommand through a shell at all: with
        # FORK_NOT_SUPPORTED it hands the whole string to posix_spawnp, which
        # reaches CreateProcessW with lpApplicationName=NULL. So no redirection,
        # no `sh -c`, and no VAR=value prefix — anything shell-shaped would be
        # passed to rtunnel as an extra argument and rejected. Quoting each token
        # keeps a leading `"`, which is the branch of OpenSSH's
        # build_commandline_string() that forwards the string unmodified.
        # `quiet` has no effect here; rtunnel's stderr goes wherever ssh's does,
        # and _proxy_env() reaches it through the environment ssh was spawned
        # with (see build_ssh_process_env).
        return " ".join(_windows_quote(part) for part in (rtunnel_bin, ws_url, "stdio://%h:%p"))

    base_cmd = f"{shlex.quote(str(rtunnel_bin))} {shlex.quote(ws_url)} {shlex.quote('stdio://%h:%p')}"
    base_cmd = _prepend_proxy_env(base_cmd)
    if quiet:
        # Wrap in sh -c to redirect stderr, suppressing rtunnel's verbose output
        cmd = f"{base_cmd} 2>/dev/null"
        return f"sh -c {shlex.quote(cmd)}"
    return base_cmd


def _windows_quote(value: object) -> str:
    """Quote one ProxyCommand token for CreateProcessW's command-line parser."""
    return '"' + str(value).replace('"', '\\"') + '"'


def build_ssh_process_env() -> dict[str, str]:
    """Build the environment for a local ``ssh``/``scp`` call.

    The locale is pinned so remote login shells do not inherit an unsupported
    value (for example ``en_US.UTF-8``) through SSH env forwarding.

    On POSIX the rtunnel proxy override rides along inside the ProxyCommand as a
    ``VAR=value`` prefix. Windows has no shell there, so it has to arrive the
    other way: ssh hands its own environment to the proxy process, so setting it
    here is what reaches rtunnel.
    """
    env = os.environ.copy()
    env.update({"LC_ALL": "C", "LANG": "C"})
    if sys.platform == "win32":
        env.update(_proxy_env())
    return env


def exec_rtunnel_proxy(
    bridge: BridgeProfile,
    config: TunnelConfig,
    *,
    target_host: str = "localhost",
    target_port: int | None = None,
    quiet: bool = False,
) -> None:
    """Replace the current process with rtunnel for OpenSSH ProxyCommand.

    The caller must keep stdout clean: after exec, stdout is the SSH byte
    stream between OpenSSH and the remote notebook sshd. When *quiet* is true,
    rtunnel stderr is redirected to ``/dev/null`` immediately before exec so
    its client lifecycle logs do not appear in the user's SSH session.

    Windows has no exec: the CRT spawns a child and terminates the caller, which
    would hand OpenSSH a proxy pid that dies the moment the tunnel comes up.
    There this stays alive as a thin parent and forwards rtunnel's exit code.
    """
    _ensure_rtunnel_binary(config)
    port = int(target_port or bridge.ssh_port)
    args = [
        str(config.rtunnel_bin),
        _ws_proxy_url(bridge.proxy_url),
        f"stdio://{target_host}:{port}",
    ]
    env = os.environ.copy()
    env.update(_proxy_env())

    if sys.platform == "win32":
        completed = subprocess.run(
            args,
            env=env,
            stderr=subprocess.DEVNULL if quiet else None,
        )
        raise SystemExit(completed.returncode)

    saved_stderr_fd: int | None = None
    if quiet:
        saved_stderr_fd = os.dup(2)
        try:
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull_fd, 2)
            finally:
                os.close(devnull_fd)
        except Exception:
            os.close(saved_stderr_fd)
            raise

    try:
        os.execve(args[0], args, env)
    except Exception:
        if saved_stderr_fd is not None:
            try:
                os.dup2(saved_stderr_fd, 2)
            finally:
                os.close(saved_stderr_fd)
        raise


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
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 5,
            env=build_ssh_process_env(),
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
