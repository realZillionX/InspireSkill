"""Pure Notebook target-cache readers and bridge lookup helpers."""

from __future__ import annotations
import logging
import json
from pathlib import Path
from typing import Any, Callable
from dataclasses import dataclass
from inspire.accounts import current_account, account_exists
from inspire.bridge import tunnel as tunnel_module
from inspire.bridge.tunnel import BridgeProfile, TunnelConfig

logger = logging.getLogger(__name__)

CACHE_VERSION = 2
TARGET_CACHE_FILENAME = "notebook-targets.json"


@dataclass
class NotebookTargetCandidate:
    account: str | None
    config: TunnelConfig
    bridge: BridgeProfile


def target_cache_path() -> Path:
    return Path.home() / ".inspire" / TARGET_CACHE_FILENAME


def notebook_target_cache_key(
    notebook: str,
    workspace: str | None,
    account: str | None = None,
    *,
    current: Callable[[], str | None] = current_account,
) -> str:
    identifier = str(notebook or "").strip()
    workspace_key = str(workspace or "").strip()
    return f"{identifier}|workspace={workspace_key}|account={effective_account(account, current=current) or ''}"


def split_target_cache_key(key: str) -> tuple[str, str]:
    key = key.rsplit("|account=", 1)[0]
    marker = "|workspace="
    if marker not in key:
        return key, ""
    identifier, workspace = key.split(marker, 1)
    return identifier, workspace


def read_target_cache(*, path: Path | None = None) -> dict[str, Any]:
    path = path or target_cache_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"version": CACHE_VERSION, "targets": {}}
    if not isinstance(data, dict):
        return {"version": CACHE_VERSION, "targets": {}}
    targets = data.get("targets")
    normalized = {}
    if isinstance(targets, dict):
        for key, entry in targets.items():
            if not isinstance(entry, dict):
                continue
            notebook, workspace = split_target_cache_key(str(key))
            # Keep existing selections while adding the account to their key.
            account = str(entry.get("account") or "")
            normalized[f"{notebook}|workspace={workspace}|account={account}"] = entry
    data["targets"] = normalized
    data["version"] = CACHE_VERSION
    return data


def effective_account(
    explicit: str | None, *, current: Callable[[], str | None] = current_account
) -> str | None:
    account = str(explicit or "").strip()
    if account:
        return account
    return current()


def account_scope(
    account: str | None,
    *,
    exists: Callable[[str], bool] = account_exists,
    current: Callable[[], str | None] = current_account,
) -> list[str]:
    selector = effective_account(account, current=current)
    if not selector:
        return []
    if not exists(selector):
        raise ValueError(f"Account not found: {selector}")
    return [selector]


def matches_workspace(bridge: BridgeProfile, workspace: str | None) -> bool:
    requested = str(workspace or "").strip()
    if not requested or requested.lower() == "all":
        return True
    return requested == str(bridge.workspace_name or "").strip()


def matches_notebook(bridge: BridgeProfile, notebook: str) -> bool:
    requested = str(notebook or "").strip()
    if not requested:
        return False
    return requested in {
        str(bridge.name or "").strip(),
        str(bridge.notebook_name or "").strip(),
    }


def candidate_from_cache_entry(
    *,
    entry: object,
    notebook: str,
    workspace: str | None,
    exists: Callable[[str], bool] = account_exists,
) -> NotebookTargetCandidate | None:
    if not isinstance(entry, dict):
        return None
    account = str(entry.get("account") or "").strip() or None
    if account and not exists(account):
        return None
    bridge_name = str(entry.get("bridge_name") or "").strip()
    notebook_id = str(entry.get("notebook_id") or "").strip()
    try:
        config = (
            tunnel_module.load_tunnel_config(account=account)
            if account
            else tunnel_module.load_tunnel_config()
        )
    except Exception:
        logger.debug("Cached target tunnel configuration load failed; trying next strategy", exc_info=True)
        return None
    try:
        bridge = config.get_bridge(bridge_name) if bridge_name else None
        if bridge is None and notebook_id and hasattr(config, "list_bridges"):
            for candidate in config.list_bridges():
                if str(candidate.notebook_id or "").strip() == notebook_id:
                    bridge = candidate
                    break
    except Exception:
        logger.debug("Cached target bridge lookup failed; trying next strategy", exc_info=True)
        return None
    if bridge is None:
        return None
    if not matches_notebook(bridge, notebook):
        return None
    if not matches_workspace(bridge, workspace):
        return None
    return NotebookTargetCandidate(account=account, config=config, bridge=bridge)


def find_candidates(
    *,
    notebook: str,
    workspace: str | None,
    account: str | None,
    exists: Callable[[str], bool] = account_exists,
    current: Callable[[], str | None] = current_account,
) -> list[NotebookTargetCandidate]:
    candidates: list[NotebookTargetCandidate] = []
    for account_name in account_scope(account, exists=exists, current=current):
        config = tunnel_module.load_tunnel_config(account=account_name)
        if not hasattr(config, "list_bridges"):
            continue
        for bridge in config.list_bridges():
            if not matches_notebook(bridge, notebook):
                continue
            if not matches_workspace(bridge, workspace):
                continue
            candidates.append(
                NotebookTargetCandidate(
                    account=account_name,
                    config=config,
                    bridge=bridge,
                )
            )
    return candidates


def target_available(candidate: NotebookTargetCandidate) -> bool:
    try:
        return tunnel_module.is_tunnel_available(
            bridge_name=candidate.bridge.name,
            config=candidate.config,
            retries=0,
            retry_pause=0.0,
            progressive=False,
        )
    except Exception:
        logger.debug("Cached target tunnel availability probe failed; trying next strategy", exc_info=True)
        return False
