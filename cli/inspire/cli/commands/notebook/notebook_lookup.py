"""Helpers for notebook lookup, ownership checks, and workspace discovery."""

from __future__ import annotations

import concurrent.futures
import logging
from typing import Any, Callable, TypeVar

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import (
    forget_resource_identity,
    looks_like_platform_id,
    run_with_stale_handle_retry,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.resource_index import (
    ResourceIdentity,
    ResourceIndex,
    ResourceScope,
    StaleResourceIndexRefresh,
    scope_for_session,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.platform.web.session import TransientAPIError

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

_ZERO_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"

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


def _positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def _dict_value(item: dict, key: str) -> Any:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _normalize_gpu_type_for_display(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    upper = text.upper().replace("_", " ").replace("-", " ")
    if upper == "CPU":
        return ""
    if "H200" in upper:
        return "H200"
    if "H100" in upper:
        return "H100"
    if "4090" in upper:
        return "4090"
    if "3090" in upper:
        return "3090"
    if "A800" in upper:
        return "A800"
    if "A100" in upper:
        return "A100"
    if "L40S" in upper:
        return "L40S"
    if "V100" in upper:
        return "V100"
    if "PPU" in upper or "ZW810" in upper:
        return "PPU ZW810"

    if "(" in text:
        text = text.split("(", maxsplit=1)[0].strip()
    if text.upper().startswith("NVIDIA "):
        text = text[7:].strip()
    if text.upper().startswith("RTX "):
        text = text[4:].strip()
    return text


def _notebook_gpu_type(item: dict) -> str:
    resource_spec_price = _dict_value(item, "resource_spec_price")
    gpu_info = _dict_value(resource_spec_price, "gpu_info")
    quota = _dict_value(item, "quota")
    resource_spec = _dict_value(item, "resource_spec")
    node_gpu_info = _dict_value(_dict_value(item, "node"), "gpu_info")
    logic_compute_group = _dict_value(item, "logic_compute_group")
    compute_group = _dict_value(item, "compute_group")

    candidates = [
        gpu_info.get("gpu_product_simple"),
        gpu_info.get("gpu_type_display"),
        gpu_info.get("brand_name"),
        gpu_info.get("gpu_type"),
        resource_spec_price.get("gpu_type_display"),
        resource_spec_price.get("gpu_type"),
        quota.get("gpu_type"),
        resource_spec.get("gpu_type_display"),
        resource_spec.get("gpu_type"),
        node_gpu_info.get("gpu_product_simple"),
        node_gpu_info.get("gpu_type_display"),
        node_gpu_info.get("brand_name"),
        node_gpu_info.get("gpu_type"),
        item.get("gpu_product_simple"),
        item.get("gpu_type_display"),
        item.get("gpu_type"),
        logic_compute_group.get("gpu_type"),
        logic_compute_group.get("gpu_type_display"),
        logic_compute_group.get("name"),
        logic_compute_group.get("logic_compute_group_name"),
        compute_group.get("gpu_type"),
        compute_group.get("gpu_type_display"),
        compute_group.get("name"),
    ]
    for candidate in candidates:
        gpu_type = _normalize_gpu_type_for_display(candidate)
        if gpu_type:
            return gpu_type
    return ""


def _notebook_compute_group(item: dict) -> str:
    """Return the notebook's compute group name, e.g. ``训练区-H200-1号机房``."""
    logic_compute_group = _dict_value(item, "logic_compute_group")
    compute_group = _dict_value(item, "compute_group")

    candidates = [
        logic_compute_group.get("name"),
        logic_compute_group.get("logic_compute_group_name"),
        compute_group.get("name"),
        compute_group.get("compute_group_name"),
        item.get("logic_compute_group_name"),
        item.get("compute_group_name"),
        item.get("compute_group"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            continue
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _looks_like_notebook_id(value: str) -> bool:
    return looks_like_platform_id(value)


def _notebook_id_from_item(item: dict) -> str | None:
    notebook_id = item.get("notebook_id") or item.get("id")
    if not notebook_id:
        return None
    return str(notebook_id)


def _format_notebook_gpu(item: dict) -> str:
    quota = item.get("quota") or {}
    gpu_count = _positive_int(quota.get("gpu_count"))

    if gpu_count:
        gpu_type = _notebook_gpu_type(item) or "GPU"
        return scrub_raw_ids(f"{gpu_count}x {gpu_type}")
    return "-"


def _format_notebook_cpu(item: dict) -> str:
    quota = item.get("quota") or {}
    cpu_count = _positive_int(quota.get("cpu_count"))
    if cpu_count:
        return f"{cpu_count} CPU"
    return "-"


def _format_notebook_resource(item: dict) -> str:
    gpu = _format_notebook_gpu(item)
    cpu = _format_notebook_cpu(item)
    if gpu != "-" and cpu != "-":
        return f"{gpu} + {cpu}"
    if gpu != "-":
        return gpu
    if cpu != "-":
        return cpu
    return "N/A"


def _current_user_lookup_failure_message(session: web_session_module.WebSession) -> str:
    del session
    return (
        "Cannot determine the current platform account. "
        "Refresh the account session with `inspire account add` or `inspire init`, "
        "then retry."
    )


def _try_get_current_user_ids(
    session: web_session_module.WebSession,
    *,
    base_url: str,
) -> list[str]:
    """Resolve the signed-in account's user id, or ``[]`` if it cannot be read.

    Callers turn ``[]`` into "the account could not be identified", so a
    platform that is merely rate limiting raises instead: telling the user
    their session is broken would send them to re-login over a wait.
    """
    try:
        data = browser_api_module.get_current_user(session=session)
        if isinstance(data, dict):
            session.user_detail = data
            try:
                session.save()
            except Exception:
                pass
        user_id = (data.get("id") or data.get("user_id")) if isinstance(data, dict) else None
        if user_id:
            return [str(user_id)]
        logger.debug(
            "Current platform account response omitted its internal identifier"
        )
    except TransientAPIError:
        raise
    except Exception:
        logger.debug("Current platform account lookup failed", exc_info=True)
    return []


def _get_current_user_detail(
    session: web_session_module.WebSession,
    *,
    base_url: str,
) -> dict:
    data = browser_api_module.get_current_user(session=session)
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
        return False, "The current account is not allowed for this notebook."

    if owner_names and current_username and current_username not in owner_names:
        return False, "The current account does not match this notebook."

    return True, ""


def _list_notebooks_for_workspace(
    session: web_session_module.WebSession,
    *,
    workspace_id: str,
    user_ids: list[str],
    keyword: str = "",
    page_size: int = 100,
    max_pages: int = 100,
    status: list[str] | None = None,
) -> list[dict]:
    if not user_ids:
        raise ValueError("Cannot list notebooks without a current-user filter.")

    page_size = max(1, int(page_size))
    max_pages = max(1, int(max_pages))
    all_items: list[dict] = []
    current_page = 1
    total: int | None = None

    while current_page <= max_pages:
        items, page_total = browser_api_module.list_notebooks(
            workspace_id,
            user_ids=user_ids,
            keyword=keyword,
            status=status,
            page=current_page,
            page_size=page_size,
            session=session,
        )
        if not items:
            break

        all_items.extend(items)

        if total is None:
            total = page_total
        if total is not None and current_page * page_size >= total:
            break
        if len(items) < page_size:
            break
        current_page += 1

    return all_items


def _list_notebooks_for_workspaces(
    session: web_session_module.WebSession,
    *,
    workspace_ids: list[str],
    user_ids: list[str],
    keyword: str = "",
    page_size: int = 100,
    max_pages: int = 100,
    status: list[str] | None = None,
    errors: dict[str, Exception] | None = None,
) -> dict[str, list[dict]]:
    if not workspace_ids:
        return {}
    if len(workspace_ids) == 1:
        ws_id = workspace_ids[0]
        return {
            ws_id: _list_notebooks_for_workspace(
                session,
                workspace_id=ws_id,
                user_ids=user_ids,
                keyword=keyword,
                page_size=page_size,
                max_pages=max_pages,
                status=status,
            )
        }

    results: dict[str, list[dict]] = {}

    def _fetch(ws_id: str) -> tuple[str, list[dict]]:
        return (
            ws_id,
            _list_notebooks_for_workspace(
                session,
                workspace_id=ws_id,
                user_ids=user_ids,
                keyword=keyword,
                page_size=page_size,
                max_pages=max_pages,
                status=status,
            ),
        )

    max_workers = min(len(workspace_ids), 4)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(_fetch, ws_id): ws_id for ws_id in workspace_ids}
        for future in concurrent.futures.as_completed(future_map):
            ws_id = future_map[future]
            try:
                ws_result_id, items = future.result()
            except Exception as e:
                if errors is None:
                    raise
                errors[ws_id] = e
                continue
            results[ws_result_id or ws_id] = items

    return results


def _collect_workspace_ids_for_lookup(
    session: web_session_module.WebSession,
) -> list[str]:
    """Enumerate workspaces in which to look up a notebook by name.

    User-facing query and lifecycle commands pass explicit workspace IDs from
    ``--workspace <name|all>``. SSH setup uses the authenticated session's
    workspace list only when no workspace selector was supplied.
    """
    candidates: list[str] = []
    all_workspace_ids = getattr(session, "all_workspace_ids", None)
    if isinstance(all_workspace_ids, list):
        candidates.extend(str(value) for value in all_workspace_ids if value)
    return _unique_workspace_ids(candidates)


def _workspace_label(session: web_session_module.WebSession, workspace_id: str) -> str:
    names = getattr(session, "all_workspace_names", None)
    if isinstance(names, dict):
        name = names.get(workspace_id)
        if name:
            return str(name)
    return "(workspace name unavailable)"


def _resolve_notebook_target(
    ctx: Context,
    *,
    session: web_session_module.WebSession,
    base_url: str,
    identifier: str,
    json_output: bool,
    workspace_ids: list[str] | None = None,
    pick: int | None = None,
    require_live: bool = False,
    cache_index: ResourceIndex | None = None,
) -> tuple[str, str | None, str]:
    """Resolve a notebook name to ``(handle, workspace_id, compute_group)``.

    The compute group rides along because every path that produces a handle --
    the identity cache and the platform list response -- already carries it.
    Callers that gate on it (notebook SSH transport policy) therefore need no
    extra detail request.
    """
    identifier = identifier.strip()
    if not identifier:
        _handle_error(
            ctx,
            "ValidationError",
            "Notebook name cannot be empty",
            EXIT_VALIDATION_ERROR,
        )

    # Names are the CLI boundary. Reject copied platform values before lookup.
    if _looks_like_notebook_id(identifier):
        _handle_error(
            ctx,
            "ValidationError",
            "CLI commands take a notebook name.",
            EXIT_VALIDATION_ERROR,
            hint=(
                "Use `inspire notebook list --workspace <workspace|all>` to find the name. "
                "Normal notebook commands resolve the name internally."
            ),
        )

    workspace_ids = workspace_ids or _collect_workspace_ids_for_lookup(session)

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

    cache_scopes: dict[str, ResourceScope] = {}
    for workspace_id in workspace_ids:
        scope = scope_for_session(
            session,
            resource_type="notebook",
            workspace_id=workspace_id,
            owner_scope="self",
            base_url=base_url,
        )
        if scope is not None:
            cache_scopes[workspace_id] = scope
    if cache_index is None and cache_scopes:
        try:
            cache_index = ResourceIndex.for_account()
        except Exception:
            logger.debug("Notebook identity cache initialization failed", exc_info=True)
            cache_index = None

    matches: list[tuple[str, dict]] = []
    if cache_index is not None and cache_scopes and not require_live:
        try:
            for workspace_id, scope in cache_scopes.items():
                for cached_item in cache_index.lookup(scope, identifier):
                    matches.append(
                        (
                            workspace_id,
                            {
                                "notebook_id": cached_item.resource_id,
                                "name": cached_item.name,
                                "status": cached_item.status,
                                "created_at": cached_item.created_at,
                                "compute_group": cached_item.compute_group,
                            },
                        )
                    )
        except Exception:
            logger.debug("Notebook identity cache lookup failed", exc_info=True)
            matches = []

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
    if not matches:
        snapshot_tokens: dict[str, tuple[int, int]] = {}
        if cache_index is not None:
            for workspace_id, scope in cache_scopes.items():
                try:
                    snapshot_tokens[workspace_id] = cache_index.snapshot_token(scope)
                except Exception:
                    logger.debug(
                        "Notebook identity cache revision read failed",
                        exc_info=True,
                    )

        user_ids = _try_get_current_user_ids(session, base_url=base_url)
        if not user_ids:
            _handle_error(
                ctx,
                "AuthenticationError",
                _current_user_lookup_failure_message(session),
                EXIT_API_ERROR,
            )

        import time as _time

        attempts = 4  # 0s, 2s, 4s, 6s — covers ~12s of eventual consistency
        for attempt in range(attempts):
            workspace_items = _list_notebooks_for_workspaces(
                session,
                workspace_ids=workspace_ids,
                user_ids=user_ids,
                keyword=identifier,
            )
            matches = []
            for ws_id in workspace_ids:
                for notebook_item in workspace_items.get(ws_id, []):
                    if str(notebook_item.get("name") or "") == identifier:
                        matches.append((ws_id, notebook_item))

            if matches:
                break
            if attempt < attempts - 1:
                _time.sleep(2 * (attempt + 1))

        if cache_index is not None and cache_scopes:
            stale_workspaces: set[str] = set()
            current_matches: list[tuple[str, dict]] = []
            for workspace_id, scope in cache_scopes.items():
                token = snapshot_tokens.get(workspace_id)
                if token is None:
                    continue
                try:
                    cache_index.replace_name(
                        scope,
                        identifier,
                        [
                            ResourceIdentity(
                                resource_id=str(
                                    _notebook_id_from_item(notebook_item) or ""
                                ),
                                name=identifier,
                                owner_id=str(
                                    notebook_item.get("user_id")
                                    or notebook_item.get("owner_id")
                                    or notebook_item.get("creator_id")
                                    or ""
                                ),
                                status=str(notebook_item.get("status") or ""),
                                created_at=str(notebook_item.get("created_at") or ""),
                                compute_group=_notebook_compute_group(notebook_item),
                            )
                            for match_workspace_id, notebook_item in matches
                            if match_workspace_id == workspace_id
                        ],
                        expected_generation=token[0],
                        expected_revision=token[1],
                    )
                except StaleResourceIndexRefresh:
                    try:
                        if cache_index.generation() != token[0]:
                            continue
                    except Exception:
                        pass
                    stale_workspaces.add(workspace_id)
                    try:
                        current_matches.extend(
                            (
                                workspace_id,
                                {
                                    "notebook_id": item.resource_id,
                                    "name": item.name,
                                    "status": item.status,
                                    "created_at": item.created_at,
                                    "compute_group": item.compute_group,
                                },
                            )
                            for item in cache_index.lookup(scope, identifier)
                        )
                    except Exception:
                        logger.debug(
                            "Notebook identity cache race recovery failed",
                            exc_info=True,
                        )
                except Exception:
                    logger.debug(
                        "Notebook identity cache refresh failed",
                        exc_info=True,
                    )
            if stale_workspaces:
                matches = [
                    match
                    for match in matches
                    if match[0] not in stale_workspaces
                ]
                matches.extend(current_matches)

    matches.sort(key=lambda m: str(m[1].get("created_at") or ""), reverse=True)

    if not matches:
        _handle_error(
            ctx,
            "APIError",
            f"Notebook not found: {identifier}",
            EXIT_API_ERROR,
            hint="Run 'inspire notebook list --workspace all' to find the notebook name.",
        )

    def _target_for(match: tuple[str, dict]) -> tuple[str, str | None, str]:
        ws_id, item = match
        notebook_id = _notebook_id_from_item(item)
        if not notebook_id:
            _handle_error(
                ctx,
                "APIError",
                f"Notebook '{identifier}' is missing a required API field.",
                EXIT_API_ERROR,
            )
            raise RuntimeError("unreachable")
        return notebook_id, ws_id, _notebook_compute_group(item)

    if len(matches) == 1:
        return _target_for(matches[0])

    if pick is not None:
        if pick < 1 or pick > len(matches):
            _handle_error(
                ctx,
                "ValidationError",
                (
                    f"--pick {pick} out of range; {len(matches)} notebooks "
                    f"share the name {identifier!r}."
                ),
                EXIT_VALIDATION_ERROR,
            )
        return _target_for(matches[pick - 1])

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
    return _target_for(matches[choice - 1])


def _resolve_notebook_id(
    ctx: Context,
    *,
    session: web_session_module.WebSession,
    base_url: str,
    identifier: str,
    json_output: bool,
    workspace_ids: list[str] | None = None,
    pick: int | None = None,
    require_live: bool = False,
    cache_index: ResourceIndex | None = None,
) -> tuple[str, str | None]:
    notebook_id, workspace_id, _compute_group = _resolve_notebook_target(
        ctx,
        session=session,
        base_url=base_url,
        identifier=identifier,
        json_output=json_output,
        workspace_ids=workspace_ids,
        pick=pick,
        require_live=require_live,
        cache_index=cache_index,
    )
    return notebook_id, workspace_id


def _run_notebook_operation_with_stale_handle_retry(
    ctx: Context,
    *,
    session: web_session_module.WebSession,
    base_url: str,
    identifier: str,
    json_output: bool,
    workspace_ids: list[str] | None,
    operation: Callable[[str], _T],
    pick: int | None = None,
    cache_index: ResourceIndex | None = None,
) -> tuple[_T, str, str | None]:
    """Run one notebook operation and recover once from an explicit stale handle."""
    resolved: dict[str, str | None] = {"handle": None, "workspace_id": None}

    def _resolve(*, require_live: bool) -> str:
        handle, workspace_id = _resolve_notebook_id(
            ctx,
            session=session,
            base_url=base_url,
            identifier=identifier,
            json_output=json_output,
            workspace_ids=workspace_ids,
            pick=pick,
            require_live=require_live,
            cache_index=cache_index,
        )
        resolved["handle"] = handle
        resolved["workspace_id"] = workspace_id
        return handle

    def _invalidate(handle: str) -> None:
        workspace_id = str(resolved.get("workspace_id") or "").strip()
        candidate_workspace_ids = [workspace_id] if workspace_id else list(workspace_ids or [])
        for candidate_workspace_id in candidate_workspace_ids:
            forget_resource_identity(
                session=session,
                resource_type="notebook",
                resource_id=handle,
                workspace_id=candidate_workspace_id,
                owner_scope="self",
                cache_index=cache_index,
            )

    result = run_with_stale_handle_retry(
        name=identifier,
        resolve_cached=lambda: _resolve(require_live=False),
        resolve_live=lambda _name: _resolve(require_live=True),
        operation=operation,
        invalidate=_invalidate,
    )
    handle = str(resolved.get("handle") or "")
    return result, handle, resolved.get("workspace_id")


__all__ = [
    "_ZERO_WORKSPACE_ID",
    "_collect_workspace_ids_for_lookup",
    "_current_user_lookup_failure_message",
    "_format_notebook_resource",
    "_get_current_user_detail",
    "_list_notebooks_for_workspace",
    "_looks_like_notebook_id",
    "_notebook_compute_group",
    "_notebook_id_from_item",
    "_resolve_notebook_id",
    "_resolve_notebook_target",
    "_run_notebook_operation_with_stale_handle_retry",
    "_sort_notebook_items",
    "_try_get_current_user_ids",
    "_unique_workspace_ids",
    "_validate_notebook_account_access",
]
