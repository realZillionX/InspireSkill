"""Cached Notebook SSH target resolution within one selected account."""

from __future__ import annotations

import logging
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

import click

from inspire.accounts import account_exists, current_account
from inspire.accounts.cache_lock import exclusive_cache_lock
from inspire.bridge import tunnel as tunnel_module  # noqa: F401
from inspire.bridge.tunnel import BridgeProfile, TunnelConfig
from inspire.cli.context import Context, EXIT_CONFIG_ERROR
from inspire.cli.formatters import human_formatter
from inspire.services.utils import json_formatter
from inspire.cli.utils.id_resolver import NAME_PICK_HELP, reject_id_at_boundary
from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.config import ConfigError
from inspire.config.workspaces import validate_workspace_operation_name

from inspire.services.notebook.notebook_output import sanitize_public_text

from inspire.services.execution import notebook_targets
from inspire.services.execution.notebook_targets import (  # noqa: F401
    NotebookTargetCandidate as NotebookTargetCandidate,
    target_cache_path as target_cache_path,
    split_target_cache_key as _split_target_cache_key,
    target_available as _target_available,
)

logger = logging.getLogger(__name__)

CACHE_VERSION = 2
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
class NotebookConnectionTarget:
    account: str | None
    config: TunnelConfig
    bridge: BridgeProfile
    source: str





def notebook_target_cache_key(notebook: str, workspace: str | None, account: str | None = None) -> str:
    return notebook_targets.notebook_target_cache_key(notebook, workspace, account, current=current_account)





def _read_target_cache() -> dict[str, Any]:
    return notebook_targets.read_target_cache(path=target_cache_path())


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
    return notebook_targets.effective_account(explicit, current=current_account)


def remember_notebook_target(
    *,
    notebook: str,
    workspace: str | None,
    account: str | None,
    bridge: BridgeProfile,
) -> None:
    account = _effective_account(account)
    identifier = str(notebook or "").strip()
    if not identifier:
        return
    with exclusive_cache_lock(target_cache_path()):
        data = _read_target_cache()
        targets = data.setdefault("targets", {})
        if not isinstance(targets, dict):
            targets = {}
            data["targets"] = targets
        key = notebook_target_cache_key(identifier, workspace, account)
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
    account = current_account()
    for key, raw_entry in sorted(targets.items()):
        if not isinstance(raw_entry, dict) or raw_entry.get("account") != account:
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

    requested_account = _effective_account(account)
    if (str(entry.get("account") or "").strip() or None) != requested_account:
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
    return notebook_targets.account_scope(account, exists=account_exists, current=current_account)








def _candidate_from_cache_entry(*, entry: object, notebook: str, workspace: str | None) -> NotebookTargetCandidate | None:
    return notebook_targets.candidate_from_cache_entry(entry=entry, notebook=notebook, workspace=workspace, exists=account_exists)


def _find_candidates(*, notebook: str, workspace: str | None, account: str | None) -> list[NotebookTargetCandidate]:
    return notebook_targets.find_candidates(notebook=notebook, workspace=workspace, account=account, exists=account_exists, current=current_account)


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
        logger.debug("Interactive prompt TTY detection failed; trying next strategy", exc_info=True)
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
    """Resolve a cached notebook bridge within the selected account.

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

    selector = _effective_account(account)
    if selector:
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
    if pick is None and not ignore_target_cache:
        data = _read_target_cache()
        entry = (data.get("targets") or {}).get(notebook_target_cache_key(notebook, workspace, selector))
        candidate = _candidate_from_cache_entry(
            entry=entry,
            notebook=notebook,
            workspace=workspace,
        )
        if candidate is not None and candidate.account == selector:
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
        account=selector,
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
