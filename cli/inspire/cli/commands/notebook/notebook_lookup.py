"""Helpers for notebook lookup, ownership checks, and workspace discovery."""

from __future__ import annotations

import concurrent.futures
import logging
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
from inspire.cli.utils.id_resolver import is_partial_id
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.platform.web import session as web_session_module

logger = logging.getLogger(__name__)

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
    value = value.strip().lower()
    if not value:
        return False
    # Keep this list in sync with ``_looks_like_platform_id`` in
    # ``inspire.cli.utils.id_resolver`` — both abbreviations (``nb-``) and
    # full prefixes (``notebook-``) round-trip through the platform.
    if value.startswith("notebook-") or value.startswith("nb-"):
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
        return scrub_raw_ids(f"{gpu_count}x{gpu_type}")

    cpu_count = quota.get("cpu_count", 0)
    if cpu_count:
        return f"{cpu_count}xCPU"
    return "N/A"


def _try_get_current_user_ids(
    session: web_session_module.WebSession,
    *,
    base_url: str,
) -> list[str]:
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
                session.save()
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
            session.save()
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
            "The current user is not allowed for this notebook.",
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
    if not user_ids:
        raise ValueError("Cannot list notebooks without a current-user filter.")

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
    """Enumerate workspaces in which to look up a notebook by name.

    Name lookup uses the live workspace IDs already attached to the
    authenticated web session, falling back to the session's current workspace
    when the platform did not enumerate the full visible set.
    """
    del config
    candidates: list[str] = []
    all_workspace_ids = getattr(session, "all_workspace_ids", None)
    if isinstance(all_workspace_ids, list):
        candidates.extend(str(value) for value in all_workspace_ids if value)
    current_workspace_id = getattr(session, "workspace_id", None)
    if current_workspace_id:
        candidates.append(str(current_workspace_id))
    return _unique_workspace_ids(candidates)


def _workspace_label(session: web_session_module.WebSession, workspace_id: str) -> str:
    names = getattr(session, "all_workspace_names", None)
    if isinstance(names, dict):
        name = names.get(workspace_id)
        if name:
            return str(name)
    return "(workspace name unavailable)"


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
            "Notebook name cannot be empty",
            EXIT_VALIDATION_ERROR,
        )

    # Names are the CLI boundary. Reject copied platform values before lookup.
    if _looks_like_notebook_id(identifier) or is_partial_id(identifier, prefix="notebook-"):
        _handle_error(
            ctx,
            "ValidationError",
            "CLI commands take a notebook name.",
            EXIT_VALIDATION_ERROR,
            hint=(
                "Use `inspire notebook list` to find the name. "
                "Use `inspire notebook id <name>` only for explicit platform lookup."
            ),
        )

    workspace_ids = _collect_workspace_ids_for_lookup(session, config)

    if not workspace_ids:
        _handle_error(
            ctx,
            "ConfigError",
            "No workspace available for notebook lookup.",
            EXIT_CONFIG_ERROR,
            hint=(
                "Run `inspire config context` to list visible workspace names, "
                "or pass --workspace <workspace-name> explicitly."
            ),
        )

    user_ids = _try_get_current_user_ids(session, base_url=base_url)
    if not user_ids:
        _handle_error(
            ctx,
            "AuthenticationError",
            "Cannot determine the current user from the live web session.",
            EXIT_API_ERROR,
        )

    # Retry the listing a few times when the name doesn't show up: the
    # platform list API has a small eventual-consistency window after a
    # fresh `notebook create` (~5-10 s of "list call SUCCEEDED but the new
    # notebook isn't in the page yet"). Without this, a `create` immediately
    # followed by `stop` / `status` / `delete` by name would 404 on the
    # user even though the notebook IS being created.
    #
    # Critically: only that "successful response, target not present" case
    # is retryable. Network errors, malformed responses, and platform
    # `code != 0` envelopes propagate immediately — otherwise we'd amplify
    # a transient real failure into a misleading 12-second wall ending in
    # "Notebook not found". The retry exists for eventual consistency on
    # the *contents* of a successful response, not as a generic error loop.
    import time as _time

    matches: list[tuple[str, dict]] = []
    attempts = 4  # 0s, 2s, 4s, 6s — covers ~12s of eventual consistency
    for attempt in range(attempts):
        workspace_items = _list_notebooks_for_workspaces(
            session,
            base_url=base_url,
            workspace_ids=workspace_ids,
            user_ids=user_ids,
            keyword=identifier,
        )
        matches = []
        for ws_id in workspace_ids:
            for item in workspace_items.get(ws_id, []):
                if str(item.get("name") or "") == identifier:
                    matches.append((ws_id, item))

        if matches:
            break
        if attempt < attempts - 1:
            _time.sleep(2 * (attempt + 1))

    matches.sort(key=lambda m: str(m[1].get("created_at") or ""), reverse=True)

    if not matches:
        _handle_error(
            ctx,
            "APIError",
            f"Notebook not found: {identifier}",
            EXIT_API_ERROR,
            hint="Run 'inspire notebook list --all-workspaces' to find the notebook name.",
        )

    if len(matches) == 1:
        ws_id, item = matches[0]
        notebook_id = _notebook_id_from_item(item)
        if not notebook_id:
            _handle_error(
                ctx,
                "APIError",
                f"Notebook '{identifier}' is missing a required API field.",
                EXIT_API_ERROR,
            )
            raise RuntimeError("unreachable")
        return notebook_id, ws_id

    def _label_for(item: dict, ws_id: str) -> str:
        status = str(item.get("status") or "Unknown")
        resource = _format_notebook_resource(item)
        created_at = str(item.get("created_at") or "")
        workspace = _workspace_label(session, ws_id)
        return scrub_raw_ids(
            f"{status:<12} {resource:<12} created_at={created_at}  workspace={workspace}"
        )

    if json_output:
        labels = [_label_for(item, ws_id) for ws_id, item in matches]
        _handle_error(
            ctx,
            "AmbiguousName",
            f"Multiple notebooks match name '{scrub_raw_ids(identifier)}':\n"
            + "\n".join(f"  [{i}] {lbl}" for i, lbl in enumerate(labels, start=1)),
            EXIT_VALIDATION_ERROR,
            hint=(
                "Rename one of the duplicates so each notebook has a unique name — "
                "normal CLI commands resolve by name."
            ),
        )

    click.echo(f"Multiple notebooks named '{scrub_raw_ids(identifier)}' found:")
    for idx, (ws_id, item) in enumerate(matches, start=1):
        click.echo(f"  [{idx}] {_label_for(item, ws_id)}")

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
            f"Notebook '{identifier}' is missing a required API field.",
            EXIT_API_ERROR,
        )
        raise RuntimeError("unreachable")
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
    "_sort_notebook_items",
    "_try_get_current_user_ids",
    "_unique_workspace_ids",
    "_validate_notebook_account_access",
]
