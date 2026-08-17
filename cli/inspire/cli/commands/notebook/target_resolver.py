"""Cross-account cached notebook SSH target resolution."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from inspire.accounts import account_exists, current_account, list_accounts
from inspire.accounts.cache_lock import exclusive_cache_lock
from inspire.bridge import tunnel as tunnel_module
from inspire.bridge.tunnel import BridgeProfile, TunnelConfig
from inspire.cli.context import Context, EXIT_CONFIG_ERROR
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.id_resolver import NAME_PICK_HELP, reject_id_at_boundary
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import ConfigError
from inspire.config.workspaces import validate_workspace_operation_name

from .public_output import sanitize_public_text

CACHE_VERSION = 1
TARGET_CACHE_FILENAME = "notebook-targets.json"
NOTEBOOK_TARGET_WORKSPACE_HELP = (
    "Workspace name used to disambiguate this notebook target."
)


def validate_specific_workspace(
    _ctx: click.Context,
    _param: click.Parameter,
    value: str | None,
) -> str | None:
    """Validate an optional single-workspace selector for cached transports."""
    if value is None or not value.strip():
        return None
    try:
        return validate_workspace_operation_name(value)
    except ConfigError as exc:
        raise click.BadParameter(str(exc)) from exc


@dataclass
class NotebookTargetCandidate:
    account: str | None
    config: TunnelConfig
    bridge: BridgeProfile


@dataclass
class NotebookConnectionTarget:
    account: str | None
    config: TunnelConfig
    bridge: BridgeProfile
    source: str


def target_cache_path() -> Path:
    return Path.home() / ".inspire" / TARGET_CACHE_FILENAME


def notebook_target_cache_key(notebook: str, workspace: str | None) -> str:
    identifier = str(notebook or "").strip()
    workspace_key = str(workspace or "").strip()
    return f"{identifier}|workspace={workspace_key}"


def _split_target_cache_key(key: str) -> tuple[str, str]:
    marker = "|workspace="
    if marker not in key:
        return key, ""
    identifier, workspace = key.split(marker, 1)
    return identifier, workspace


def _read_target_cache() -> dict[str, Any]:
    path = target_cache_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"version": CACHE_VERSION, "targets": {}}
    if not isinstance(data, dict):
        return {"version": CACHE_VERSION, "targets": {}}
    targets = data.get("targets")
    if not isinstance(targets, dict):
        data["targets"] = {}
    data["version"] = CACHE_VERSION
    return data


def _write_target_cache(data: dict[str, Any]) -> None:
    path = target_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["version"] = CACHE_VERSION
    payload.setdefault("targets", {})
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _effective_account(explicit: str | None) -> str | None:
    account = str(explicit or "").strip()
    if account and account.lower() != "all":
        return account
    return current_account()


def remember_notebook_target(
    *,
    notebook: str,
    workspace: str | None,
    account: str | None,
    bridge: BridgeProfile,
) -> None:
    identifier = str(notebook or "").strip()
    if not identifier:
        return
    with exclusive_cache_lock(target_cache_path()):
        data = _read_target_cache()
        targets = data.setdefault("targets", {})
        if not isinstance(targets, dict):
            targets = {}
            data["targets"] = targets
        key = notebook_target_cache_key(identifier, workspace)
        targets[key] = {
            "account": account,
            "bridge_name": bridge.name,
            "notebook_name": bridge.notebook_name,
            "notebook_id": bridge.notebook_id,
            "workspace_name": bridge.workspace_name,
            "workspace_id": bridge.workspace_id,
            "updated_at": int(time.time()),
        }
        _write_target_cache(data)


def remember_notebook_target_aliases(
    *,
    requested_identifier: str,
    workspace: str | None,
    account: str | None,
    bridge: BridgeProfile,
) -> None:
    effective = _effective_account(account)
    identifiers = [str(requested_identifier or "").strip()]
    notebook_name = str(bridge.notebook_name or "").strip()
    bridge_name = str(bridge.name or "").strip()
    for candidate in (notebook_name, bridge_name):
        if candidate and candidate not in identifiers:
            identifiers.append(candidate)
    for identifier in identifiers:
        remember_notebook_target(
            notebook=identifier,
            workspace=workspace,
            account=effective,
            bridge=bridge,
        )


def list_notebook_targets() -> list[dict[str, Any]]:
    data = _read_target_cache()
    targets = data.get("targets") or {}
    if not isinstance(targets, dict):
        return []
    rows: list[dict[str, Any]] = []
    for key, raw_entry in sorted(targets.items()):
        if not isinstance(raw_entry, dict):
            continue
        identifier, _workspace_key = _split_target_cache_key(str(key))
        name = sanitize_public_text(
            raw_entry.get("notebook_name") or identifier,
            omit_urls=True,
        ) or "(unknown)"
        rows.append(
            {
                "name": name,
                "account": sanitize_public_text(raw_entry.get("account"), omit_urls=True),
                "workspace": sanitize_public_text(
                    raw_entry.get("workspace_name"),
                    omit_urls=True,
                ),
                "updated_at": raw_entry.get("updated_at"),
            }
        )
    return rows


def _target_entry_matches(
    *,
    key: str,
    entry: object,
    notebook: str | None,
    workspace: str | None,
    account: str | None,
    bridge_name: str | None,
    notebook_id: str | None,
) -> bool:
    if not isinstance(entry, dict):
        return False

    identifier, workspace_key = _split_target_cache_key(str(key))
    requested_notebook = str(notebook or "").strip()
    if requested_notebook:
        notebook_values = {
            identifier,
            str(entry.get("bridge_name") or "").strip(),
            str(entry.get("notebook_name") or "").strip(),
        }
        if requested_notebook not in notebook_values:
            return False

    requested_workspace = str(workspace or "").strip()
    if requested_workspace and requested_workspace.lower() != "all":
        workspace_values = {
            workspace_key,
            str(entry.get("workspace_name") or "").strip(),
        }
        if requested_workspace not in workspace_values:
            return False

    requested_account = str(account or "").strip()
    if requested_account and requested_account.lower() != "all":
        if str(entry.get("account") or "").strip() != requested_account:
            return False

    requested_bridge = str(bridge_name or "").strip()
    if requested_bridge and str(entry.get("bridge_name") or "").strip() != requested_bridge:
        return False

    requested_notebook_id = str(notebook_id or "").strip()
    if (
        requested_notebook_id
        and str(entry.get("notebook_id") or "").strip() != requested_notebook_id
    ):
        return False

    return True


def forget_notebook_targets(
    *,
    notebook: str | None = None,
    workspace: str | None = None,
    account: str | None = None,
    bridge_name: str | None = None,
    notebook_id: str | None = None,
) -> list[str]:
    with exclusive_cache_lock(target_cache_path()):
        data = _read_target_cache()
        targets = data.get("targets") or {}
        if not isinstance(targets, dict) or not targets:
            return []

        removed: list[str] = []
        kept: dict[str, Any] = {}
        for key, entry in targets.items():
            if _target_entry_matches(
                key=str(key),
                entry=entry,
                notebook=notebook,
                workspace=workspace,
                account=account,
                bridge_name=bridge_name,
                notebook_id=notebook_id,
            ):
                removed.append(str(key))
            else:
                kept[str(key)] = entry

        if removed:
            data["targets"] = kept
            _write_target_cache(data)
        return removed


def _account_scope(account: str | None) -> list[str]:
    selector = str(account or "").strip()
    if selector and selector.lower() != "all":
        if not account_exists(selector):
            raise ValueError(f"Account not found: {selector}")
        return [selector]

    accounts = list_accounts()
    active = current_account()
    ordered: list[str] = []
    if active and active in accounts:
        ordered.append(active)
    for name in accounts:
        if name not in ordered:
            ordered.append(name)
    return ordered


def _matches_workspace(bridge: BridgeProfile, workspace: str | None) -> bool:
    requested = str(workspace or "").strip()
    if not requested or requested.lower() == "all":
        return True
    return requested == str(bridge.workspace_name or "").strip()


def _matches_notebook(bridge: BridgeProfile, notebook: str) -> bool:
    requested = str(notebook or "").strip()
    if not requested:
        return False
    return requested in {
        str(bridge.name or "").strip(),
        str(bridge.notebook_name or "").strip(),
    }


def _candidate_from_cache_entry(
    *,
    entry: object,
    notebook: str,
    workspace: str | None,
) -> NotebookTargetCandidate | None:
    if not isinstance(entry, dict):
        return None
    account = str(entry.get("account") or "").strip() or None
    if account and not account_exists(account):
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
        return None
    try:
        bridge = config.get_bridge(bridge_name) if bridge_name else None
        if bridge is None and notebook_id and hasattr(config, "list_bridges"):
            for candidate in config.list_bridges():
                if str(candidate.notebook_id or "").strip() == notebook_id:
                    bridge = candidate
                    break
    except Exception:
        return None
    if bridge is None:
        return None
    if not _matches_notebook(bridge, notebook):
        return None
    if not _matches_workspace(bridge, workspace):
        return None
    return NotebookTargetCandidate(account=account, config=config, bridge=bridge)


def _find_candidates(
    *,
    notebook: str,
    workspace: str | None,
    account: str | None,
) -> list[NotebookTargetCandidate]:
    candidates: list[NotebookTargetCandidate] = []
    for account_name in _account_scope(account):
        config = tunnel_module.load_tunnel_config(account=account_name)
        if not hasattr(config, "list_bridges"):
            continue
        for bridge in config.list_bridges():
            if not _matches_notebook(bridge, notebook):
                continue
            if not _matches_workspace(bridge, workspace):
                continue
            candidates.append(
                NotebookTargetCandidate(
                    account=account_name,
                    config=config,
                    bridge=bridge,
                )
            )
    return candidates


def _candidate_label(candidate: NotebookTargetCandidate, index: int | None = None) -> str:
    bridge = candidate.bridge
    parts: list[str] = []
    if index is not None:
        parts.append(f"[{index}]")
    parts.extend(
        [
            f"account={sanitize_public_text(candidate.account, omit_urls=True) or '(none)'}",
            "notebook="
            f"{sanitize_public_text(bridge.notebook_name, omit_urls=True) or '(unknown)'}",
            f"workspace={sanitize_public_text(bridge.workspace_name, omit_urls=True) or '(unknown)'}",
        ]
    )
    return "  ".join(parts)


def _candidate_hint(candidates: list[NotebookTargetCandidate]) -> str:
    lines = ["Candidates:"]
    lines.extend(_candidate_label(candidate, index=i) for i, candidate in enumerate(candidates, 1))
    lines.append(
        "Pass `--pick <n>` to select one explicitly, `--workspace <name>` or "
        "`--account <name>` to narrow the candidates, or retry interactively."
    )
    return "\n".join(lines)


def _can_prompt(ctx: Context) -> bool:
    if ctx.json_output:
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stderr.isatty())
    except Exception:
        return False


def _emit_ambiguous_and_exit(
    ctx: Context,
    *,
    notebook: str,
    candidates: list[NotebookTargetCandidate],
) -> None:
    message = f"Multiple cached notebook connections match '{notebook}'."
    hint = _candidate_hint(candidates)
    if ctx.json_output:
        click.echo(
            json_formatter.format_json_error(
                "AmbiguousTarget",
                message,
                EXIT_CONFIG_ERROR,
                hint=hint,
            ),
            err=True,
        )
    else:
        click.echo(
            human_formatter.format_error(
                scrub_raw_ids(message),
                hint=scrub_raw_ids(hint),
            ),
            err=True,
        )
    raise SystemExit(EXIT_CONFIG_ERROR)


def _select_candidate(
    ctx: Context,
    *,
    notebook: str,
    workspace: str | None,
    candidates: list[NotebookTargetCandidate],
    allow_prompt: bool,
    pick: int | None,
) -> NotebookConnectionTarget:
    if pick is not None:
        if pick < 1 or pick > len(candidates):
            _handle_pick_out_of_range(ctx, notebook=notebook, pick=pick, candidates=candidates)
        candidate = candidates[pick - 1]
        remember_notebook_target(
            notebook=notebook,
            workspace=workspace,
            account=candidate.account,
            bridge=candidate.bridge,
        )
        return NotebookConnectionTarget(
            account=candidate.account,
            config=candidate.config,
            bridge=candidate.bridge,
            source="pick",
        )

    if len(candidates) == 1:
        candidate = candidates[0]
        remember_notebook_target(
            notebook=notebook,
            workspace=workspace,
            account=candidate.account,
            bridge=candidate.bridge,
        )
        return NotebookConnectionTarget(
            account=candidate.account,
            config=candidate.config,
            bridge=candidate.bridge,
            source="bridge_cache",
        )

    if allow_prompt and _can_prompt(ctx):
        click.echo("Multiple cached notebook connections matched:", err=True)
        for index, candidate in enumerate(candidates, 1):
            click.echo(_candidate_label(candidate, index=index), err=True)
        choice = click.prompt(
            "Select notebook target",
            type=click.IntRange(1, len(candidates)),
            err=True,
        )
        candidate = candidates[int(choice) - 1]
        remember_notebook_target(
            notebook=notebook,
            workspace=workspace,
            account=candidate.account,
            bridge=candidate.bridge,
        )
        return NotebookConnectionTarget(
            account=candidate.account,
            config=candidate.config,
            bridge=candidate.bridge,
            source="prompt",
        )

    _emit_ambiguous_and_exit(ctx, notebook=notebook, candidates=candidates)
    raise RuntimeError("unreachable")


def _handle_pick_out_of_range(
    ctx: Context,
    *,
    notebook: str,
    pick: int,
    candidates: list[NotebookTargetCandidate],
) -> None:
    message = (
        f"--pick {pick} out of range; {len(candidates)} cached notebook connections "
        f"match {notebook!r}."
    )
    if ctx.json_output:
        click.echo(
            json_formatter.format_json_error(
                "ValidationError",
                message,
                EXIT_CONFIG_ERROR,
                hint=NAME_PICK_HELP,
            ),
            err=True,
        )
    else:
        click.echo(
            human_formatter.format_error(
                scrub_raw_ids(message),
                hint=NAME_PICK_HELP,
            ),
            err=True,
        )
    raise SystemExit(EXIT_CONFIG_ERROR)


def _target_available(candidate: NotebookTargetCandidate) -> bool:
    try:
        return tunnel_module.is_tunnel_available(
            bridge_name=candidate.bridge.name,
            config=candidate.config,
            retries=0,
            retry_pause=0.0,
            progressive=False,
        )
    except Exception:
        return False


def resolve_cached_notebook_target(
    ctx: Context,
    *,
    notebook: str,
    workspace: str | None,
    account: str | None = None,
    ignore_target_cache: bool = False,
    verify_target_cache: bool = True,
    allow_prompt: bool = True,
    pick: int | None = None,
) -> NotebookConnectionTarget | None:
    """Resolve a cached notebook bridge across configured accounts.

    Returns ``None`` when no matching cached bridge exists. Ambiguous matches
    either prompt or exit with a candidate list.
    """
    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list",
    )
    if workspace:
        workspace = reject_id_at_boundary(
            ctx,
            workspace,
            resource_type="workspace",
            list_command="inspire account context",
        )
    if pick is not None and pick < 1:
        _handle_pick_out_of_range(
            ctx,
            notebook=notebook,
            pick=pick,
            candidates=[],
        )

    selector = str(account or "").strip()
    if selector and selector.lower() != "all":
        try:
            _account_scope(selector)
        except ValueError as exc:
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json_error(
                        "ConfigError",
                        str(exc),
                        EXIT_CONFIG_ERROR,
                    ),
                    err=True,
                )
            else:
                click.echo(human_formatter.format_error(str(exc)), err=True)
            raise SystemExit(EXIT_CONFIG_ERROR) from exc

    require_candidate_verification = False
    # An explicit pick is an instruction to select from the current candidate
    # set. Do not let a previously remembered target silently override it.
    if pick is None and not selector and not ignore_target_cache:
        data = _read_target_cache()
        entry = (data.get("targets") or {}).get(notebook_target_cache_key(notebook, workspace))
        candidate = _candidate_from_cache_entry(
            entry=entry,
            notebook=notebook,
            workspace=workspace,
        )
        if candidate is not None:
            if verify_target_cache and not _target_available(candidate):
                if not ctx.json_output:
                    click.echo(
                        ("Cached notebook target is unavailable; rediscovering cached candidates."),
                        err=True,
                    )
                require_candidate_verification = True
            else:
                return NotebookConnectionTarget(
                    account=candidate.account,
                    config=candidate.config,
                    bridge=candidate.bridge,
                    source="target_cache",
                )

    candidates = _find_candidates(
        notebook=notebook,
        workspace=workspace,
        account=None if selector.lower() == "all" else selector or None,
    )
    if not candidates:
        return None
    if verify_target_cache and require_candidate_verification:
        candidates = [candidate for candidate in candidates if _target_available(candidate)]
        if not candidates:
            return None
    return _select_candidate(
        ctx,
        notebook=notebook,
        workspace=workspace,
        candidates=candidates,
        allow_prompt=allow_prompt,
        pick=pick,
    )


__all__ = [
    "NOTEBOOK_TARGET_WORKSPACE_HELP",
    "NotebookConnectionTarget",
    "NotebookTargetCandidate",
    "forget_notebook_targets",
    "list_notebook_targets",
    "notebook_target_cache_key",
    "remember_notebook_target",
    "remember_notebook_target_aliases",
    "resolve_cached_notebook_target",
    "target_cache_path",
    "validate_specific_workspace",
]
