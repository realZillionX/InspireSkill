"""Tunnel domain models and errors."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class TunnelError(Exception):
    """Base exception for tunnel-related errors."""


class TunnelNotAvailableError(TunnelError):
    """Raised when tunnel is not available or not running."""


class BridgeNotFoundError(TunnelError):
    """Raised when specified bridge profile is not found."""


# Default configuration
DEFAULT_SSH_USER = "root"
DEFAULT_SSH_PORT = 22222
DEFAULT_RTUNNEL_PORT = 31337
_PROXY_PORT_RE = re.compile(r"/proxy/(\d+)/")


def _coerce_rtunnel_port(value: object) -> Optional[int]:
    if value in (None, ""):
        return None
    if not isinstance(value, (str, bytes, bytearray, int)):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    if port <= 0 or port > 65535:
        return None
    return port


def _proxy_port_from_url(proxy_url: str) -> Optional[int]:
    match = _PROXY_PORT_RE.search(str(proxy_url))
    if not match:
        return None
    return _coerce_rtunnel_port(match.group(1))


@dataclass
class BridgeProfile:
    """A single bridge configuration."""

    name: str
    proxy_url: str
    ssh_user: str = DEFAULT_SSH_USER
    ssh_port: int = DEFAULT_SSH_PORT
    # Optional notebook binding for saved notebook SSH profiles.
    notebook_id: Optional[str] = None
    notebook_name: Optional[str] = None
    workspace_id: Optional[str] = None
    workspace_name: Optional[str] = None
    identity_file: Optional[str] = None
    # Optional rtunnel server port in the notebook.
    rtunnel_port: Optional[int] = None

    def to_dict(self) -> dict:
        payload = {
            "name": self.name,
            "proxy_url": self.proxy_url,
            "ssh_user": self.ssh_user,
            "ssh_port": self.ssh_port,
        }
        if self.notebook_id:
            payload["notebook_id"] = self.notebook_id
        if self.notebook_name:
            payload["notebook_name"] = self.notebook_name
        if self.workspace_id:
            payload["workspace_id"] = self.workspace_id
        if self.workspace_name:
            payload["workspace_name"] = self.workspace_name
        if self.identity_file:
            payload["identity_file"] = self.identity_file
        if self.rtunnel_port is not None:
            payload["rtunnel_port"] = self.rtunnel_port
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "BridgeProfile":
        proxy_url = data["proxy_url"]
        rtunnel_port = (
            _coerce_rtunnel_port(data.get("rtunnel_port"))
            or _proxy_port_from_url(proxy_url)
            or DEFAULT_RTUNNEL_PORT
        )
        return cls(
            name=data["name"],
            proxy_url=proxy_url,
            ssh_user=data.get("ssh_user", DEFAULT_SSH_USER),
            ssh_port=data.get("ssh_port", DEFAULT_SSH_PORT),
            notebook_id=data.get("notebook_id"),
            notebook_name=data.get("notebook_name"),
            workspace_id=data.get("workspace_id"),
            workspace_name=data.get("workspace_name"),
            identity_file=data.get("identity_file"),
            rtunnel_port=rtunnel_port,
        )


@dataclass
class TunnelConfig:
    """Tunnel configuration with multiple bridge profiles."""

    bridges: dict[str, BridgeProfile] = field(default_factory=dict)
    default_bridge: Optional[str] = None
    account: Optional[str] = None

    # Paths
    config_dir: Path = field(default_factory=lambda: Path.home() / ".inspire")

    @property
    def config_file(self) -> Path:
        if self.account:
            return self.config_dir / "accounts" / self.account / "bridges.json"
        return self.config_dir / "bridges.json"

    @property
    def rtunnel_bin(self) -> Path:
        return Path.home() / ".local" / "bin" / "rtunnel"

    def get_bridge(self, name: Optional[str] = None) -> Optional[BridgeProfile]:
        """Get a bridge profile by name, or the default if name is None."""
        if name:
            return self.bridges.get(name)
        if self.default_bridge:
            return self.bridges.get(self.default_bridge)
        if len(self.bridges) == 1:
            # If only one bridge, use it as default
            return next(iter(self.bridges.values()))
        return None

    def add_bridge(self, profile: BridgeProfile) -> None:
        """Add or update a bridge profile."""
        self.bridges[profile.name] = profile
        # Set as default if it's the first bridge
        if self.default_bridge is None:
            self.default_bridge = profile.name

    def remove_bridge(self, name: str) -> bool:
        """Remove a bridge profile. Returns True if removed."""
        if name in self.bridges:
            del self.bridges[name]
            if self.default_bridge == name:
                # Set new default
                self.default_bridge = next(iter(self.bridges.keys()), None)
            return True
        return False

    def list_bridges(self) -> list[BridgeProfile]:
        """List all bridge profiles."""
        return list(self.bridges.values())
