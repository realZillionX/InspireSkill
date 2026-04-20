"""SCP file transfer via ProxyCommand: upload and download files to/from Bridge."""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

from .models import BridgeProfile
from .ssh_exec import _resolve_bridge_and_proxy

logger = logging.getLogger(__name__)


def _build_scp_base_args(
    *,
    bridge: BridgeProfile,
    proxy_cmd: str,
    recursive: bool = False,
) -> list[str]:
    args = [
        "scp",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        f"ProxyCommand={proxy_cmd}",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "BatchMode=yes",
        "-P",
        str(bridge.ssh_port),
    ]
    if recursive:
        args.append("-r")
    return args


def run_scp_transfer(
    local_path: str,
    remote_path: str,
    *,
    download: bool = False,
    recursive: bool = False,
    bridge_name: Optional[str] = None,
    config=None,
    timeout: Optional[int] = None,
) -> subprocess.CompletedProcess:
    """Transfer files to/from Bridge via SCP over ProxyCommand.

    Args:
        local_path: Local file or directory path.
        remote_path: Remote file or directory path on Bridge.
        download: If True, download from remote to local. Default is upload.
        recursive: If True, copy directories recursively.
        bridge_name: Optional bridge profile name.
        config: Optional TunnelConfig (loaded automatically if None).
        timeout: Optional timeout in seconds.

    Returns:
        subprocess.CompletedProcess with the SCP result.
    """
    _config, bridge, proxy_cmd = _resolve_bridge_and_proxy(bridge_name, config)

    args = _build_scp_base_args(
        bridge=bridge,
        proxy_cmd=proxy_cmd,
        recursive=recursive,
    )

    remote_spec = f"{bridge.ssh_user}@localhost:{remote_path}"

    if download:
        args.extend([remote_spec, local_path])
    else:
        args.extend([local_path, remote_spec])

    logger.debug(
        "run_scp_transfer bridge=%s download=%s recursive=%s timeout=%s",
        bridge.name,
        download,
        recursive,
        timeout,
    )
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    logger.debug(
        "run_scp_transfer completed bridge=%s returncode=%s stdout_chars=%s stderr_chars=%s",
        bridge.name,
        result.returncode,
        len(result.stdout or ""),
        len(result.stderr or ""),
    )
    if result.stdout:
        logger.debug("run_scp_transfer stdout:\n%s", result.stdout)
    if result.stderr:
        logger.debug("run_scp_transfer stderr:\n%s", result.stderr)
    return result


__all__ = [
    "_build_scp_base_args",
    "run_scp_transfer",
]
