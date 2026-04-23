"""Tunnel configuration file management.

Storage layout — one account = one isolated file:

    ~/.inspire/accounts/<account>/bridges.json   # when an account is active
    ~/.inspire/bridges.json                      # when no account is set

Active account is resolved from ``inspire.accounts.current_account()``
(which reads the single-line ``~/.inspire/current`` pointer). An explicit
``account=`` kwarg overrides that, and nothing else.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .models import BridgeProfile, TunnelConfig

logger = logging.getLogger(__name__)


def _resolve_active_account(explicit: Optional[str]) -> Optional[str]:
    """Pick the active account for bridge storage.

    Order:
      1. *explicit* parameter (caller knows better than global state)
      2. ``~/.inspire/current`` via :mod:`inspire.accounts`
      3. ``None`` — bridges live at the unscoped ``~/.inspire/bridges.json``
    """
    candidate = (explicit or "").strip()
    if candidate:
        return candidate

    try:
        from inspire.accounts import current_account
    except ImportError:  # pragma: no cover - accounts module ships with the CLI
        return None
    return current_account()


def _read_json_into_config(path: Path, config: TunnelConfig) -> Optional[str]:
    """Load bridges from ``path`` into ``config``. Returns the stored default
    name (or None). Existing bridges win on name collision (first loader to
    register a name keeps it)."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    for bridge_data in data.get("bridges", []):
        try:
            profile = BridgeProfile.from_dict(bridge_data)
        except (KeyError, TypeError):
            continue
        config.bridges.setdefault(profile.name, profile)

    default_name = str(data.get("default") or "").strip()
    return default_name or None


def load_tunnel_config(
    config_dir: Optional[Path] = None,
    account: Optional[str] = None,
) -> TunnelConfig:
    """Load the bridge-profile set for the given or active account."""
    resolved_account = _resolve_active_account(account)

    config = TunnelConfig(account=resolved_account)
    if config_dir is not None:
        config.config_dir = config_dir

    config.config_dir.mkdir(parents=True, exist_ok=True)

    preferred_default: Optional[str] = None

    primary_path = config.config_file
    if primary_path.exists():
        preferred_default = _read_json_into_config(primary_path, config)

    if preferred_default and preferred_default in config.bridges:
        config.default_bridge = preferred_default
    elif config.bridges:
        config.default_bridge = next(iter(config.bridges.keys()))

    return config


def save_tunnel_config(config: TunnelConfig) -> None:
    """Save tunnel configuration to the account-scoped ``bridges.json``.

    Creates parent directories as needed (e.g. ``accounts/<name>/``).
    """
    target = config.config_file
    target.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "default": config.default_bridge,
        "bridges": [p.to_dict() for p in config.bridges.values()],
    }

    with open(target, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
