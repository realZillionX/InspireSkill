"""SSH tunnel utilities (ProxyCommand + rtunnel).

This package contains the tunnel domain logic used by the CLI (tunnel management
and ssh execution over the rtunnel-backed ProxyCommand).
"""

from __future__ import annotations

from .config import load_tunnel_config, save_tunnel_config
from .models import (
    BridgeNotFoundError,
    BridgeProfile,
    DEFAULT_SSH_PORT,
    DEFAULT_SSH_USER,
    TunnelConfig,
    TunnelError,
    TunnelNotAvailableError,
    has_internet_for_gpu_type,
)
from .rtunnel import (
    DEFAULT_RTUNNEL_DOWNLOAD_URL,
    _ensure_rtunnel_binary,
    _get_rtunnel_download_url,
    get_rtunnel_path,
)
from .ssh import (
    _get_proxy_command,
    _test_ssh_connection,
    get_tunnel_status,
    is_tunnel_available,
)
from .scp import run_scp_transfer
from .ssh_exec import (
    get_ssh_command_args,
    run_ssh_command,
    run_ssh_command_streaming,
)

__all__ = [
    # Models / errors
    "BridgeNotFoundError",
    "BridgeProfile",
    "DEFAULT_SSH_PORT",
    "DEFAULT_SSH_USER",
    "TunnelConfig",
    "TunnelError",
    "TunnelNotAvailableError",
    "has_internet_for_gpu_type",
    # Config
    "load_tunnel_config",
    "save_tunnel_config",
    # rtunnel
    "DEFAULT_RTUNNEL_DOWNLOAD_URL",
    "_ensure_rtunnel_binary",
    "_get_rtunnel_download_url",
    "get_rtunnel_path",
    # SSH helpers
    "_get_proxy_command",
    "_test_ssh_connection",
    "get_ssh_command_args",
    "get_tunnel_status",
    "is_tunnel_available",
    "run_scp_transfer",
    "run_ssh_command",
    "run_ssh_command_streaming",
]
