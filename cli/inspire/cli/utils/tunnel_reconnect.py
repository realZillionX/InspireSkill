"""Reconnect/rebuild helpers for SSH tunnels backed by notebook rtunnel."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from inspire.bridge.tunnel import BridgeProfile, TunnelConfig, save_tunnel_config
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import WebSession

DEFAULT_RTUNNEL_PORT = 31337
SSH_DISCONNECT_RETURN_CODES = {255}
_PROXY_PORT_RE = re.compile(r"/proxy/(\d+)/")


class NotebookBridgeReconnectStatus(str, Enum):
    """Outcome of a single notebook bridge rebuild attempt."""

    REBUILT = "rebuilt"
    RETRY_LATER = "retry_later"
    EXHAUSTED = "exhausted"
    NOT_REBUILDABLE = "not_rebuildable"


@dataclass
class NotebookBridgeReconnectState:
    """Mutable reconnect state shared across repeated rebuild attempts."""

    reconnect_limit: int
    reconnect_pause: float
    reconnect_attempt: int = 0
    web_session: Optional[WebSession] = None
    ssh_public_key: str = ""


@dataclass
class NotebookBridgeReconnectResult:
    """Result of a reconnect/rebuild attempt."""

    status: NotebookBridgeReconnectStatus
    attempt: int
    error: Exception | None = None
    pause_seconds: float = 0.0


def extract_rtunnel_port(proxy_url: str, *, default_port: int = DEFAULT_RTUNNEL_PORT) -> int:
    """Extract `/proxy/<port>/` from a proxy URL; fall back to *default_port*."""
    match = _PROXY_PORT_RE.search(str(proxy_url))
    if match:
        try:
            port = int(match.group(1))
            if 0 < port <= 65535:
                return port
        except ValueError:
            pass
    return default_port


def should_attempt_ssh_reconnect(
    returncode: int,
    *,
    interactive: bool,
    allow_non_interactive: bool = False,
) -> bool:
    """Return True if this SSH exit code indicates connection loss."""
    if returncode not in SSH_DISCONNECT_RETURN_CODES:
        return False
    return interactive or allow_non_interactive


def retry_pause_seconds(attempt: int, *, base_pause: float, progressive: bool = True) -> float:
    """Compute pause before retrying reconnect/rebuild."""
    base = max(0.0, float(base_pause))
    if not progressive:
        return base
    return base + float(max(0, attempt - 1))


def load_ssh_public_key_material(pubkey_path: Optional[str] = None) -> str:
    """Load SSH public key content from an explicit or default local path."""
    if pubkey_path:
        candidates = [Path(pubkey_path).expanduser()]
    else:
        candidates = [
            Path.home() / ".ssh" / "id_ed25519.pub",
            Path.home() / ".ssh" / "id_rsa.pub",
        ]

    for path in candidates:
        if not path.exists():
            continue
        key = path.read_text(encoding="utf-8", errors="ignore").strip()
        if key:
            return key

    raise ValueError(
        "No SSH public key found. Provide --pubkey PATH or generate one with 'ssh-keygen'."
    )


def rebuild_notebook_bridge_profile(
    *,
    bridge_name: str,
    bridge: BridgeProfile,
    tunnel_config: TunnelConfig,
    session: WebSession,
    ssh_public_key: str,
    timeout: int = 300,
    headless: bool = True,
) -> BridgeProfile:
    """Rebuild a notebook-backed bridge profile and persist it to tunnel config."""
    notebook_id = str(getattr(bridge, "notebook_id", "") or "").strip()
    if not notebook_id:
        raise ValueError(f"Bridge '{bridge_name}' is not notebook-backed (missing notebook_id).")

    tunnel_port = bridge.rtunnel_port or extract_rtunnel_port(bridge.proxy_url)
    proxy_url = browser_api_module.setup_notebook_rtunnel(
        notebook_id=notebook_id,
        port=tunnel_port,
        ssh_port=bridge.ssh_port,
        ssh_public_key=ssh_public_key,
        session=session,
        headless=headless,
        timeout=timeout,
    )

    updated = BridgeProfile(
        name=bridge_name,
        proxy_url=proxy_url,
        ssh_user=bridge.ssh_user,
        ssh_port=bridge.ssh_port,
        has_internet=bridge.has_internet,
        notebook_id=notebook_id,
        notebook_name=bridge.notebook_name,
        rtunnel_port=tunnel_port,
    )
    tunnel_config.add_bridge(updated)
    save_tunnel_config(tunnel_config)
    return updated


def attempt_notebook_bridge_rebuild(
    *,
    state: NotebookBridgeReconnectState,
    bridge_name: str,
    bridge: BridgeProfile,
    tunnel_config: TunnelConfig,
    session_loader: Callable[[], WebSession],
    rebuild_fn: Callable[..., BridgeProfile] = rebuild_notebook_bridge_profile,
    key_loader: Callable[[Optional[str]], str] = load_ssh_public_key_material,
    pubkey_path: Optional[str] = None,
    timeout: int = 300,
    headless: bool = True,
) -> NotebookBridgeReconnectResult:
    """Run one rebuild attempt and update reconnect state in place."""
    notebook_id = str(getattr(bridge, "notebook_id", "") or "").strip()
    if not notebook_id:
        return NotebookBridgeReconnectResult(
            status=NotebookBridgeReconnectStatus.NOT_REBUILDABLE,
            attempt=state.reconnect_attempt,
        )

    if state.reconnect_attempt >= state.reconnect_limit:
        return NotebookBridgeReconnectResult(
            status=NotebookBridgeReconnectStatus.EXHAUSTED,
            attempt=state.reconnect_attempt,
        )

    state.reconnect_attempt += 1
    attempt = state.reconnect_attempt

    try:
        if state.web_session is None:
            state.web_session = session_loader()
        if not state.ssh_public_key:
            state.ssh_public_key = key_loader(pubkey_path)

        rebuild_fn(
            bridge_name=bridge_name,
            bridge=bridge,
            tunnel_config=tunnel_config,
            session=state.web_session,
            ssh_public_key=state.ssh_public_key,
            timeout=timeout,
            headless=headless,
        )
        return NotebookBridgeReconnectResult(
            status=NotebookBridgeReconnectStatus.REBUILT,
            attempt=attempt,
        )
    except Exception as error:  # noqa: BLE001
        if state.reconnect_attempt >= state.reconnect_limit:
            return NotebookBridgeReconnectResult(
                status=NotebookBridgeReconnectStatus.EXHAUSTED,
                attempt=attempt,
                error=error,
            )

        return NotebookBridgeReconnectResult(
            status=NotebookBridgeReconnectStatus.RETRY_LATER,
            attempt=attempt,
            error=error,
            pause_seconds=retry_pause_seconds(
                state.reconnect_attempt,
                base_pause=state.reconnect_pause,
                progressive=True,
            ),
        )


__all__ = [
    "DEFAULT_RTUNNEL_PORT",
    "NotebookBridgeReconnectResult",
    "NotebookBridgeReconnectState",
    "NotebookBridgeReconnectStatus",
    "attempt_notebook_bridge_rebuild",
    "extract_rtunnel_port",
    "load_ssh_public_key_material",
    "rebuild_notebook_bridge_profile",
    "retry_pause_seconds",
    "should_attempt_ssh_reconnect",
]
