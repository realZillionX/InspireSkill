"""Helpers for notebook lookup, ownership checks, and workspace discovery."""

from __future__ import annotations

import concurrent.futures
import re
from typing import Any

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import is_partial_id, normalize_partial, resolve_partial_id
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import session as web_session_module

_ZERO_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"

_NOTEBOOK_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _unique_workspace_ids(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = value.strip()
        if not value or value == _ZERO_WORKSPACE_ID:
            continue
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _sort_notebook_items(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda item: str(item.get("created_at") or ""), reverse=True)


def _looks_like_notebook_id(value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    if value.startswith("notebook-"):
        return True
    return bool(_NOTEBOOK_UUID_RE.match(value))


def _notebook_id_from_item(item: dict) -> str | None:
    notebook_id = item.get("notebook_id") or item.get("id")
    if not notebook_id:
        return None
    return str(notebook_id)


def _format_notebook_resource(item: dict) -> str:
    quota = item.get("quota") or {}
    gpu_count = quota.get("gpu_count", 0)

    if gpu_count and gpu_count > 0:
        gpu_info = (item.get("resource_spec_price") or {}).get("gpu_info") or {}
        gpu_type = gpu_info.get("gpu_product_simple") or quota.get("gpu_type") or "GPU"
        return f"{gpu_count}x{gpu_type}"

    cpu_count = quota.get("cpu_count", 0)
    if cpu_count:
        return f"{cpu_count}xCPU"
    return "N/A"


def _try_get_current_user_ids(
    session: web_session_module.WebSession,
    *,
    base_url: str,
) -> list[str]:
    cached_detail = getattr(session, "user_detail", None)
    if isinstance(cached_detail, dict):
        user_id = cached_detail.get("id")
        if user_id:
            return [str(user_id)]

    try:
        user_data = web_session_module.request_json(
            session,
            "GET",
            f"{base_url}/api/v1/user/detail",
            timeout=30,
        )
        data = user_data.get("data", {})
        if isinstance(data, dict):
            session.user_detail = data
            try:
                session.save(account=session.login_username)
            except Exception:
                pass
        user_id = data.get("id")
        if user_id:
            return [str(user_id)]
    except Exception:
        pass
    return []


def _get_current_user_detail(
    session: web_session_module.WebSession,
    *,
    base_url: str,
) -> dict:
    cached_detail = getattr(session, "user_detail", None)
    if isinstance(cached_detail, dict) and cached_detail:
        return cached_detail

    user_data = web_session_module.request_json(
        session,
        "GET",
        f"{base_url}/api/v1/user/detail",
        timeout=30,
    )
    data = user_data.get("data", {}) if isinstance(user_data, dict) else {}
    if isinstance(data, dict) and data:
        session.user_detail = data
        try:
            session.save(account=session.login_username)
        except Exception:
            pass
        return data
    return {}


def _first_non_empty_str(data: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        value_str = str(value).strip()
        if value_str:
            return value_str
    return ""


def _collect_user_ids(data: dict, keys: tuple[str, ...]) -> set[str]:
    ids: set[str] = set()
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    candidate = _first_non_empty_str(item, ("id", "user_id", "uid"))
                else:
                    candidate = str(item).strip()
                if candidate:
                    ids.add(candidate)
            continue
        if isinstance(value, dict):
            candidate = _first_non_empty_str(value, ("id", "user_id", "uid"))
        else:
            candidate = str(value).strip()
        if candidate:
            ids.add(candidate)
    return ids


def _validate_notebook_account_access(
    *,
    current_user: dict,
    notebook_detail: dict,
) -> tuple[bool, str]:
    current_user_id = _first_non_empty_str(current_user, ("id", "user_id", "uid"))
    current_username = _first_non_empty_str(
        current_user,
        ("username", "user_name", "name", "email", "account"),
    )
    if not current_user_id and not current_username:
        return True, ""

    owner_ids = _collect_user_ids(
        notebook_detail,
        ("user_id", "owner_id", "creator_id", "created_by", "owner", "creator"),
    )
    member_ids = _collect_user_ids(
        notebook_detail,
        ("members", "member_list", "users", "collaborators", "authorized_users"),
    )

    owner_names = set()
    for key in ("username", "owner_username", "creator_username", "created_by_username"):
        value = notebook_detail.get(key)
        if value is None:
            continue
        value_str = str(value).strip()
        if value_str:
            owner_names.add(value_str)

    if member_ids and current_user_id and current_user_id in member_ids:
        return True, ""
    if owner_ids and current_user_id and current_user_id in owner_ids:
        return True, ""
    if owner_names and current_username and current_username in owner_names:
        return True, ""

    if (
        owner_ids
        and current_user_id
        and current_user_id not in owner_ids
        and (not member_ids or current_user_id not in member_ids)
    ):
        return (
            False,
            f"current user id '{current_user_id}' is not allowed for this notebook "
            f"(owner ids: {', '.join(sorted(owner_ids))})",
        )

    if owner_names and current_username and current_username not in owner_names:
        return (
            False,
            f"current user '{current_username}' does not match notebook owner "
            f"({', '.join(sorted(owner_names))})",
        )

    return True, ""


def _list_notebooks_for_workspace(
    session: web_session_module.WebSession,
    *,
    base_url: str,
    workspace_id: str,
    user_ids: list[str],
    keyword: str = "",
    page_size: int = 20,
    status: list[str] | None = None,
) -> list[dict]:
    body = {
        "workspace_id": workspace_id,
        "page": 1,
        "page_size": page_size,
        "filter_by": {
            "keyword": keyword,
            "user_id": user_ids,
            "logic_compute_group_id": [],
            "status": status or [],
            "mirror_url": [],
        },
        "order_by": [{"field": "created_at", "order": "desc"}],
    }

    data = web_session_module.request_json(
        session,
        "POST",
        f"{base_url}/api/v1/notebook/list",
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        message = data.get("message", "Unknown error")
        raise ValueError(f"API error: {message}")

    items = data.get("data", {}).get("list", [])
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _list_notebooks_for_workspaces(
    session: web_session_module.WebSession,
    *,
    base_url: str,
    workspace_ids: list[str],
    user_ids: list[str],
    keyword: str = "",
    page_size: int = 20,
    status: list[str] | None = None,
) -> dict[str, list[dict]]:
    if not workspace_ids:
        return {}
    if len(workspace_ids) == 1:
        ws_id = workspace_ids[0]
        return {
            ws_id: _list_notebooks_for_workspace(
                session,
                base_url=base_url,
                workspace_id=ws_id,
                user_ids=user_ids,
                keyword=keyword,
                page_size=page_size,
                status=status,
            )
        }

    results: dict[str, list[dict]] = {}

    def _fetch(ws_id: str) -> tuple[str, list[dict]]:
        return (
            ws_id,
            _list_notebooks_for_workspace(
                session,
                base_url=base_url,
                workspace_id=ws_id,
                user_ids=user_ids,
                keyword=keyword,
                page_size=page_size,
                status=status,
            ),
        )

    max_workers = min(len(workspace_ids), 4)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(_fetch, ws_id): ws_id for ws_id in workspace_ids}
        for future in concurrent.futures.as_completed(future_map):
            ws_id = future_map[future]
            ws_result_id, items = future.result()
            results[ws_result_id or ws_id] = items

    return results


def _collect_workspace_ids_for_lookup(
    session: web_session_module.WebSession,
    config: Any,
) -> list[str]:
    candidates: list[str] = []
    for ws_id in (
        getattr(config, "workspace_cpu_id", None),
        getattr(config, "workspace_gpu_id", None),
        getattr(config, "workspace_internet_id", None),
        getattr(config, "job_workspace_id", None),
    ):
        if ws_id:
            candidates.append(str(ws_id))

    workspaces_map = getattr(config, "workspaces", None)
    if isinstance(workspaces_map, dict):
        candidates.extend(str(value) for value in workspaces_map.values() if value)
    if getattr(session, "workspace_id", None):
        candidates.append(str(session.workspace_id))

    workspace_ids = _unique_workspace_ids(candidates)
    if workspace_ids:
        return workspace_ids

    resolved_ws = None
    try:
        resolved_ws = select_workspace_id(config)
    except Exception:
        resolved_ws = None

    resolved_ws = resolved_ws or getattr(session, "workspace_id", None)
    if resolved_ws and resolved_ws != _ZERO_WORKSPACE_ID:
        return [str(resolved_ws)]
    return []


def _resolve_partial_notebook_id(
    ctx: Context,
    *,
    session: web_session_module.WebSession,
    config: Any,
    base_url: str,
    partial: str,
    json_output: bool,
) -> str | None:
    workspace_ids = _collect_workspace_ids_for_lookup(session, config)
    if not workspace_ids:
        return None

    user_ids = _try_get_current_user_ids(session, base_url=base_url)
    nb_matches: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    try:
        workspace_items = _list_notebooks_for_workspaces(
            session,
            base_url=base_url,
            workspace_ids=workspace_ids,
            user_ids=user_ids,
        )
    except Exception:
        workspace_items = {}
    for ws_id in workspace_ids:
        items = workspace_items.get(ws_id, [])
        for item in items:
            nid = _notebook_id_from_item(item)
            if not nid or nid in seen_ids:
                continue
            seen_ids.add(nid)
            uuid_part = nid[9:] if nid.lower().startswith("notebook-") else nid
            if uuid_part.lower().startswith(partial):
                label = item.get("name") or item.get("status") or ""
                nb_matches.append((nid, label))

    if not nb_matches:
        return None
    return resolve_partial_id(ctx, partial, "notebook", nb_matches, json_output)


def _resolve_notebook_id(
    ctx: Context,
    *,
    session: web_session_module.WebSession,
    config: Any,
    base_url: str,
    identifier: str,
    json_output: bool,
) -> tuple[str, str | None]:
    identifier = identifier.strip()
    if not identifier:
        _handle_error(
            ctx,
            "ValidationError",
            "Notebook identifier cannot be empty",
            EXIT_VALIDATION_ERROR,
        )

    if _looks_like_notebook_id(identifier):
        return identifier, None

    if is_partial_id(identifier, prefix="notebook-"):
        partial = normalize_partial(identifier, prefix="notebook-")
        resolved_partial = _resolve_partial_notebook_id(
            ctx,
            session=session,
            config=config,
            base_url=base_url,
            partial=partial,
            json_output=json_output,
        )
        if resolved_partial:
            return resolved_partial, None

    workspace_ids = _collect_workspace_ids_for_lookup(session, config)

    if not workspace_ids:
        _handle_error(
            ctx,
            "ConfigError",
            "No workspace_id configured or available for notebook lookup.",
            EXIT_CONFIG_ERROR,
            hint=(
                "Set [workspaces].cpu/[workspaces].gpu in config.toml, set INSPIRE_WORKSPACE_ID, "
                "or pass a notebook ID directly."
            ),
        )

    user_ids: list[str] = []

    matches: list[tuple[str, dict]] = []
    try:
        workspace_items = _list_notebooks_for_workspaces(
            session,
            base_url=base_url,
            workspace_ids=workspace_ids,
            user_ids=user_ids,
            keyword=identifier,
        )
    except Exception:
        workspace_items = {}
    for ws_id in workspace_ids:
        items = workspace_items.get(ws_id, [])

        for item in items:
            raw_item_id = str(item.get("id") or "").strip()
            if raw_item_id and raw_item_id == identifier:
                matches.append((ws_id, item))
                continue
            if str(item.get("name") or "") == identifier:
                matches.append((ws_id, item))

    matches.sort(key=lambda m: str(m[1].get("created_at") or ""), reverse=True)

    if not matches:
        _handle_error(
            ctx,
            "APIError",
            f"Notebook not found: {identifier}",
            EXIT_API_ERROR,
            hint="Run 'inspire notebook list --all-workspaces' to find the notebook ID.",
        )

    if len(matches) == 1:
        ws_id, item = matches[0]
        notebook_id = _notebook_id_from_item(item)
        if not notebook_id:
            _handle_error(
                ctx,
                "APIError",
                f"Notebook '{identifier}' is missing an ID in API response.",
                EXIT_API_ERROR,
            )
        return notebook_id, ws_id

    if json_output:
        ids = [(_notebook_id_from_item(item) or "?") for _, item in matches]
        _handle_error(
            ctx,
            "ValidationError",
            f"Multiple notebooks match name '{identifier}': {', '.join(ids)}",
            EXIT_VALIDATION_ERROR,
            hint="Use a notebook ID instead of a name.",
        )

    click.echo(f"Multiple notebooks named '{identifier}' found:")
    for idx, (ws_id, item) in enumerate(matches, start=1):
        notebook_id = _notebook_id_from_item(item) or "N/A"
        status = str(item.get("status") or "Unknown")
        resource = _format_notebook_resource(item)
        created_at = str(item.get("created_at") or "")
        click.echo(f"  [{idx}] {status:<12} {resource:<12} {notebook_id}  {created_at}  ws={ws_id}")

    choice = click.prompt(
        "Select notebook",
        type=click.IntRange(1, len(matches)),
        default=1,
        show_default=True,
    )
    ws_id, item = matches[choice - 1]
    notebook_id = _notebook_id_from_item(item)
    if not notebook_id:
        _handle_error(
            ctx,
            "APIError",
            f"Notebook '{identifier}' is missing an ID in API response.",
            EXIT_API_ERROR,
        )
    return notebook_id, ws_id


__all__ = [
    "_ZERO_WORKSPACE_ID",
    "_collect_workspace_ids_for_lookup",
    "_format_notebook_resource",
    "_get_current_user_detail",
    "_list_notebooks_for_workspace",
    "_looks_like_notebook_id",
    "_notebook_id_from_item",
    "_resolve_notebook_id",
    "_resolve_partial_notebook_id",
    "_sort_notebook_items",
    "_try_get_current_user_ids",
    "_unique_workspace_ids",
    "_validate_notebook_account_access",
]
