"""Workspace-name resolution utilities."""

from __future__ import annotations

import re
from typing import Any, Optional

from inspire.cli.utils.resource_index import ResourceIdentity, ResourceIndex, scope_for_session
from inspire.config import ConfigError

_WORKSPACE_ID_RE = re.compile(
    r"^ws-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_PLACEHOLDER_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"


def _validate_workspace_id(value: str) -> None:
    if value == _PLACEHOLDER_WORKSPACE_ID:
        raise ConfigError("workspace_id is the placeholder. Pass a real workspace name.")
    if not _WORKSPACE_ID_RE.match(value):
        raise ConfigError(f"Invalid workspace_id format: {value!r}")


def _session_workspace_names(session: Any) -> dict[str, str]:
    names = getattr(session, "all_workspace_names", None)
    if isinstance(names, dict):
        return {str(wid): str(name) for wid, name in names.items() if wid and name}
    return {}


def _enumerated_workspace_names(session: Any) -> dict[str, str]:
    try:
        from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces

        items = try_enumerate_workspaces(session)
    except Exception:
        return {}

    out: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        wid = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if wid and name:
            out[wid] = name
    return out


def workspace_name_map(session: Any) -> dict[str, str]:
    """Return the visible ``workspace_id -> workspace name`` map."""
    resolved = _session_workspace_names(session)
    for wid, name in _enumerated_workspace_names(session).items():
        resolved.setdefault(wid, name)
    return resolved


def workspace_label(session: Any, workspace_id: str, requested: str | None = None) -> str:
    if requested:
        return requested
    return workspace_name_map(session).get(workspace_id) or "(workspace name unavailable)"


def _visible_workspace_ids(session: Any) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for wid in getattr(session, "all_workspace_ids", None) or []:
        wid_s = str(wid or "").strip()
        if wid_s and wid_s != _PLACEHOLDER_WORKSPACE_ID and wid_s not in seen:
            ordered.append(wid_s)
            seen.add(wid_s)
    return ordered


def resolve_workspace_query_scope(
    config: Any,
    *,
    workspace: Optional[str],
    session: Any,
) -> tuple[list[str], bool]:
    """Resolve required query scope from ``--workspace <name|all>``.

    Query commands must not inherit the browser session's active workspace.
    ``all`` is the only supported fanout sentinel.
    """
    raw = (workspace or "").strip()
    if not raw:
        raise ConfigError("Workspace is required. Pass --workspace <workspace-name|all>.")
    if raw.lower() == "current":
        raise ConfigError("--workspace current is not supported. Pass a workspace name or 'all'.")
    if raw.lower() == "all":
        workspace_ids = _visible_workspace_ids(session)
        if not workspace_ids:
            raise ConfigError("No visible workspaces found in the live web session.")
        return workspace_ids, True
    resolved = select_workspace_id(
        config,
        explicit_workspace_name=raw,
        session=session,
    )
    if not resolved:
        raise ConfigError(f"Unknown workspace name: {raw!r}.")
    return [resolved], False


def resolve_workspace_operation_scope(
    config: Any,
    *,
    workspace: Optional[str],
    session: Any,
) -> str:
    """Resolve a required single workspace name for write-like commands."""
    raw = validate_workspace_operation_name(workspace)
    resolved = select_workspace_id(
        config,
        explicit_workspace_name=raw,
        session=session,
    )
    if not resolved:
        raise ConfigError(f"Unknown workspace name: {raw!r}.")
    return resolved


def validate_workspace_operation_name(workspace: Optional[str]) -> str:
    """Validate the visible workspace name shape for write-like commands."""
    raw = (workspace or "").strip()
    if not raw:
        raise ConfigError("Workspace is required. Pass --workspace <workspace-name>.")
    if raw.lower() in {"all", "current"}:
        raise ConfigError("--workspace must be a workspace name for this command.")
    if _WORKSPACE_ID_RE.match(raw):
        raise ConfigError("--workspace takes a workspace name, not a raw workspace ID.")
    return raw


def select_workspace_id(
    config: Any,
    *,
    gpu_type: Optional[str] = None,
    cpu_only: Optional[bool] = None,
    prefer_internet: bool = False,
    explicit_workspace_id: Optional[str] = None,
    explicit_workspace_name: Optional[str] = None,
    session: Any | None = None,
) -> Optional[str]:
    """Resolve an explicit workspace name to a workspace id.

    User-facing CLI options accept workspace names only. Raw ``ws-...`` ids are
    only accepted through ``explicit_workspace_id`` for internal call sites that
    already obtained ids from the live session.
    """
    del config, gpu_type, cpu_only, prefer_internet

    if explicit_workspace_id:
        _validate_workspace_id(explicit_workspace_id)
        return explicit_workspace_id

    if explicit_workspace_name is None:
        return None

    key = explicit_workspace_name.strip()
    if not key:
        raise ConfigError("Workspace name cannot be empty")
    if key.lower() in {"all", "current"}:
        raise ConfigError("--workspace takes a workspace name, not 'all' or 'current'.")
    if _WORKSPACE_ID_RE.match(key):
        raise ConfigError("--workspace takes a workspace name, not a raw workspace ID.")

    if session is None:
        from inspire.platform.web.session import get_web_session

        session = get_web_session()

    index: ResourceIndex | None = None
    scope = scope_for_session(session, resource_type="workspace")
    if scope is not None:
        try:
            index = ResourceIndex.for_account()
            if index is not None:
                cached = index.lookup(
                    scope,
                    key,
                    case_sensitive=False,
                )
                if len(cached) == 1:
                    return cached[0].resource_id
                if len(cached) > 1:
                    raise ConfigError(f"Workspace name {key!r} is ambiguous.")
        except ConfigError:
            raise
        except Exception:
            index = None

    live_names = workspace_name_map(session)
    candidates = [
        (wid, name)
        for wid, name in live_names.items()
        if name.lower() == key.lower()
    ]
    if index is not None and scope is not None:
        try:
            index.replace_name(
                scope,
                key,
                [
                    ResourceIdentity(resource_id=workspace_id, name=name)
                    for workspace_id, name in candidates
                ],
            )
            index.upsert(
                scope,
                [
                    ResourceIdentity(resource_id=workspace_id, name=name)
                    for workspace_id, name in live_names.items()
                ],
            )
        except Exception:
            pass
    if len(candidates) == 1:
        return candidates[0][0]
    if len(candidates) > 1:
        names = ", ".join(name for _wid, name in candidates[:5])
        raise ConfigError(f"Workspace name {key!r} is ambiguous. Candidates: {names}")

    available = sorted(set(live_names.values()))
    available_hint = ", ".join(available) if available else "(no workspace names available)"
    raise ConfigError(f"Unknown workspace name: {explicit_workspace_name!r}. Available: {available_hint}")


def workspace_required_hint(config: Any | None = None) -> str:
    del config
    return (
        "pass --workspace <workspace-name>. Run `inspire config context` "
        "to list visible workspace names"
    )


__all__ = [
    "select_workspace_id",
    "resolve_workspace_operation_scope",
    "resolve_workspace_query_scope",
    "validate_workspace_operation_name",
    "workspace_label",
    "workspace_name_map",
    "workspace_required_hint",
]
