"""Tunnel configuration file management."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from .models import BridgeProfile, TunnelConfig, DEFAULT_SSH_USER

logger = logging.getLogger(__name__)


def _resolve_username_from_config() -> Optional[str]:
    """Resolve the configured Inspire username using normal config precedence."""
    try:
        from inspire.config import Config, ConfigError

        config, _ = Config.from_files_and_env(require_credentials=False)
    except (ImportError, ConfigError, OSError, ValueError, TypeError) as error:
        logger.debug("Unable to resolve username from layered config: %s", error)
        return None

    username = str(getattr(config, "username", "") or "").strip()
    return username or None


def _resolve_tunnel_account(account: Optional[str]) -> Optional[str]:
    """Resolve the bridge account key used for bridges-<account>.json."""
    explicit = (account or "").strip()
    if explicit:
        return explicit

    # Highest-priority explicit override for bridge profile selection.
    bridge_account = (os.environ.get("INSPIRE_BRIDGE_ACCOUNT") or "").strip()
    if bridge_account:
        return bridge_account

    # Prefer the resolved project/global config username.
    config_username = _resolve_username_from_config()
    if config_username:
        return config_username

    # Fallbacks for compatibility with older env-driven setups.
    env_username = (os.environ.get("INSPIRE_USERNAME") or "").strip()
    if env_username:
        return env_username

    legacy_account = (os.environ.get("INSPIRE_ACCOUNT") or "").strip()
    if legacy_account:
        return legacy_account

    return None


def _candidate_config_paths(config_dir: Path, resolved_account: Optional[str]) -> list[Path]:
    """Build config read candidates in precedence order."""
    candidates: list[Path] = []

    if resolved_account:
        candidates.append(config_dir / f"bridges-{resolved_account}.json")

    # Backward compatibility with alias-scoped files (e.g. primary/secondary).
    legacy_account = (os.environ.get("INSPIRE_ACCOUNT") or "").strip()
    if legacy_account and legacy_account != resolved_account:
        candidates.append(config_dir / f"bridges-{legacy_account}.json")

    # Legacy shared file.
    candidates.append(config_dir / "bridges.json")

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def load_tunnel_config(
    config_dir: Optional[Path] = None,
    account: Optional[str] = None,
) -> TunnelConfig:
    """Load tunnel configuration from ~/.inspire/bridges[-{account}].json.

    Account resolution order:
      1. *account* parameter (explicit)
      2. ``INSPIRE_BRIDGE_ACCOUNT`` environment variable
      3. resolved config username (project/global/env precedence)
      4. ``INSPIRE_ACCOUNT`` environment variable (legacy compatibility)
      5. ``None`` — uses legacy ``bridges.json``

    When an account is resolved, this loader reads the account-scoped file first
    and merges compatibility fallback files in order:
      - ``bridges-{account}.json``
      - ``bridges-{INSPIRE_ACCOUNT}.json`` (if different)
      - ``bridges.json`` (legacy)

    The first occurrence of each bridge name wins. Saves always target the
    resolved account file when an account is available.
    """
    resolved_account = _resolve_tunnel_account(account)

    config = TunnelConfig(account=resolved_account)
    if config_dir:
        config.config_dir = config_dir

    config.config_dir.mkdir(parents=True, exist_ok=True)

    preferred_defaults: list[str] = []

    # Read and merge new JSON format from all compatible locations.
    for read_path in _candidate_config_paths(config.config_dir, resolved_account):
        if not read_path.exists():
            continue
        try:
            with open(read_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        default_name = str(data.get("default") or "").strip()
        if default_name:
            preferred_defaults.append(default_name)

        for bridge_data in data.get("bridges", []):
            try:
                profile = BridgeProfile.from_dict(bridge_data)
            except (KeyError, TypeError):
                continue
            if profile.name in config.bridges:
                continue
            config.bridges[profile.name] = profile

    for default_name in preferred_defaults:
        if default_name in config.bridges:
            config.default_bridge = default_name
            break

    if config.default_bridge is None and config.bridges:
        config.default_bridge = next(iter(config.bridges.keys()))

    # Migrate from old format if new format is empty
    old_config_file = config.config_dir / "tunnel.conf"
    if not config.bridges and old_config_file.exists():
        proxy_url = None
        ssh_user = DEFAULT_SSH_USER
        with open(old_config_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key == "PROXY_URL":
                        proxy_url = value
                    elif key == "SSH_USER":
                        ssh_user = value

        if proxy_url:
            # Create a default bridge from old config
            profile = BridgeProfile(
                name="default",
                proxy_url=proxy_url,
                ssh_user=ssh_user,
            )
            config.add_bridge(profile)
            # Save in new format
            save_tunnel_config(config)

    return config


def save_tunnel_config(config: TunnelConfig) -> None:
    """Save tunnel configuration to ~/.inspire/bridges[-{account}].json."""
    config.config_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "default": config.default_bridge,
        "bridges": [p.to_dict() for p in config.bridges.values()],
    }

    with open(config.config_file, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
