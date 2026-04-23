"""Project subcommands."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import time
import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.formatters.human_formatter import format_epoch
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import (
    require_web_session,
    resolve_json_output,
)
from inspire.platform.web import browser_api as browser_api_module

_ZERO_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"
_PROJECT_LIST_MAX_WORKERS = 16
_PROJECT_LIST_WORKSPACE_FANOUT_LIMIT = 6
_PROJECT_LIST_CACHE_TTL_SECONDS = 60
_PROJECT_LIST_CACHE_MAX_ENTRIES = 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_to_dict(proj: browser_api_module.ProjectInfo) -> dict:
    """Convert a ProjectInfo to a plain dict for JSON output."""
    return {
        "project_id": proj.project_id,
        "name": proj.name,
        "workspace_id": proj.workspace_id,
        "budget": proj.budget,
        "remain_budget": proj.remain_budget,
        "member_remain_budget": proj.member_remain_budget,
        "gpu_limit": proj.gpu_limit,
        "member_gpu_limit": proj.member_gpu_limit,
        "priority_level": proj.priority_level,
        "priority_name": proj.priority_name,
    }


def _unique_workspace_ids(values: list[str | None]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        ws_id = str(value or "").strip()
        if not ws_id or ws_id == _ZERO_WORKSPACE_ID:
            continue
        if ws_id in seen:
            continue
        seen.add(ws_id)
        unique.append(ws_id)
    return unique


def _merge_projects(
    projects: list[browser_api_module.ProjectInfo],
    additional: list[browser_api_module.ProjectInfo],
    *,
    seen: set[str],
) -> None:
    for project in additional:
        if project.project_id not in seen:
            seen.add(project.project_id)
            projects.append(project)


def _collect_workspace_projects(
    workspace_ids: list[str],
    *,
    session,
) -> tuple[list[browser_api_module.ProjectInfo], list[tuple[str, str]]]:
    """Collect projects across workspace IDs.

    The first workspace is queried serially to establish the request mode
    (HTTP vs browser fallback). Remaining workspaces are fetched in parallel.
    Browser fallback is safe because clients are cached per-thread.
    """
    projects: list[browser_api_module.ProjectInfo] = []
    seen: set[str] = set()
    workspace_errors: list[tuple[str, str]] = []

    if not workspace_ids:
        return projects, workspace_errors

    first_ws_id = workspace_ids[0]
    try:
        first_projects = browser_api_module.list_projects(workspace_id=first_ws_id, session=session)
        _merge_projects(projects, first_projects, seen=seen)
    except Exception as exc:
        workspace_errors.append((first_ws_id, str(exc)))

    remaining_ws_ids = workspace_ids[1:]
    if not remaining_ws_ids:
        return projects, workspace_errors

    if len(remaining_ws_ids) > 1:
        max_workers = min(len(remaining_ws_ids), _PROJECT_LIST_MAX_WORKERS)
        results_by_workspace: dict[str, list[browser_api_module.ProjectInfo]] = {}
        errors_by_workspace: dict[str, str] = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    browser_api_module.list_projects, workspace_id=ws_id, session=session
                ): ws_id
                for ws_id in remaining_ws_ids
            }
            for future in concurrent.futures.as_completed(futures):
                ws_id = futures[future]
                try:
                    results_by_workspace[ws_id] = future.result()
                except Exception as exc:
                    errors_by_workspace[ws_id] = str(exc)

        for ws_id in remaining_ws_ids:
            if ws_id in errors_by_workspace:
                workspace_errors.append((ws_id, errors_by_workspace[ws_id]))
                continue
            _merge_projects(projects, results_by_workspace.get(ws_id, []), seen=seen)
        return projects, workspace_errors

    for ws_id in remaining_ws_ids:
        try:
            ws_projects = browser_api_module.list_projects(workspace_id=ws_id, session=session)
            _merge_projects(projects, ws_projects, seen=seen)
        except Exception as exc:
            workspace_errors.append((ws_id, str(exc)))
    return projects, workspace_errors


def _select_workspace_ids_for_listing(
    workspace_ids: list[str],
    *,
    session_workspace_id: str | None,
    all_workspaces: bool,
) -> list[str]:
    if all_workspaces or len(workspace_ids) <= _PROJECT_LIST_WORKSPACE_FANOUT_LIMIT:
        return workspace_ids

    selected: list[str] = []
    seen: set[str] = set()

    preferred = str(session_workspace_id or "").strip()
    if preferred and preferred in workspace_ids:
        selected.append(preferred)
        seen.add(preferred)

    for ws_id in workspace_ids:
        if ws_id in seen:
            continue
        selected.append(ws_id)
        seen.add(ws_id)
        if len(selected) >= _PROJECT_LIST_WORKSPACE_FANOUT_LIMIT:
            break

    return selected


def _project_list_cache_file(session) -> str:  # noqa: ANN001
    """Per-account project-list cache, colocated with the account's config."""
    from pathlib import Path as _Path

    from inspire.accounts import current_account

    active = current_account()
    if active:
        return str(_Path.home() / ".inspire" / "accounts" / active / "project_list.json")
    # No active account — use a throwaway location under the user's cache dir
    # so the caller can still memoize within a single run.
    return str(_Path.home() / ".cache" / "inspire-skill" / "project_list.json")


def _project_session_fingerprint(session) -> str:  # noqa: ANN001
    storage_state = getattr(session, "storage_state", None) or {}
    cookies = storage_state.get("cookies") if isinstance(storage_state, dict) else []
    payload = json.dumps(
        [
            {
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain"),
                "path": c.get("path"),
            }
            for c in cookies or []
            if isinstance(c, dict)
        ],
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _project_list_cache_key(
    *,
    session,
    mode: str,
    all_workspaces: bool,
    workspace_ids: list[str],
) -> str:
    normalized_ws_ids = sorted(
        {str(ws_id).strip() for ws_id in workspace_ids if str(ws_id).strip()}
    )
    payload = {
        "mode": mode,
        "all_workspaces": all_workspaces,
        "workspace_ids": normalized_ws_ids,
        "workspace_id": getattr(session, "workspace_id", None),
        "base_url": getattr(session, "base_url", None),
        "session_fp": _project_session_fingerprint(session),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def _project_info_to_dict(project: browser_api_module.ProjectInfo) -> dict:
    return {
        "project_id": project.project_id,
        "name": project.name,
        "workspace_id": project.workspace_id,
        "budget": project.budget,
        "remain_budget": project.remain_budget,
        "member_remain_budget": project.member_remain_budget,
        "member_remain_gpu_hours": project.member_remain_gpu_hours,
        "gpu_limit": project.gpu_limit,
        "member_gpu_limit": project.member_gpu_limit,
        "priority_level": project.priority_level,
        "priority_name": project.priority_name,
    }


def _project_info_from_dict(data: dict) -> browser_api_module.ProjectInfo:
    def _float(value) -> float:  # noqa: ANN001
        if value is None or value == "":
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    return browser_api_module.ProjectInfo(
        project_id=str(data.get("project_id", "")),
        name=str(data.get("name", "")),
        workspace_id=str(data.get("workspace_id", "")),
        budget=_float(data.get("budget")),
        remain_budget=_float(data.get("remain_budget")),
        member_remain_budget=_float(data.get("member_remain_budget")),
        member_remain_gpu_hours=_float(data.get("member_remain_gpu_hours")),
        gpu_limit=bool(data.get("gpu_limit", False)),
        member_gpu_limit=bool(data.get("member_gpu_limit", False)),
        priority_level=str(data.get("priority_level", "")),
        priority_name=str(data.get("priority_name", "")),
    )


def _load_project_cache(
    *,
    session,
    mode: str,
    all_workspaces: bool,
    workspace_ids: list[str],
) -> list[browser_api_module.ProjectInfo] | None:
    cache_file = _project_list_cache_file(session)
    now = time.time()
    key = _project_list_cache_key(
        session=session,
        mode=mode,
        all_workspaces=all_workspaces,
        workspace_ids=workspace_ids,
    )
    try:
        with open(cache_file) as f:
            payload = json.load(f)
    except Exception:
        return None

    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        return None
    entry = entries.get(key)
    if not isinstance(entry, dict):
        return None

    created_at = entry.get("created_at")
    if not isinstance(created_at, (int, float)):
        return None
    if (now - float(created_at)) > _PROJECT_LIST_CACHE_TTL_SECONDS:
        return None

    rows = entry.get("projects")
    if not isinstance(rows, list):
        return None
    projects: list[browser_api_module.ProjectInfo] = []
    try:
        for row in rows:
            if isinstance(row, dict):
                projects.append(_project_info_from_dict(row))
    except Exception:
        return None
    return projects


def _save_project_cache(
    *,
    session,
    mode: str,
    all_workspaces: bool,
    workspace_ids: list[str],
    projects: list[browser_api_module.ProjectInfo],
) -> None:
    cache_file = _project_list_cache_file(session)
    now = time.time()
    key = _project_list_cache_key(
        session=session,
        mode=mode,
        all_workspaces=all_workspaces,
        workspace_ids=workspace_ids,
    )

    payload: dict = {"version": 1, "entries": {}}
    try:
        with open(cache_file) as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            payload = loaded
    except Exception:
        pass

    entries = payload.get("entries")
    if not isinstance(entries, dict):
        entries = {}

    # Drop stale entries before writing.
    fresh_entries: dict[str, dict] = {}
    for entry_key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        created_at = entry.get("created_at")
        if not isinstance(created_at, (int, float)):
            continue
        if (now - float(created_at)) <= (_PROJECT_LIST_CACHE_TTL_SECONDS * 2):
            fresh_entries[str(entry_key)] = entry

    fresh_entries[key] = {
        "created_at": now,
        "projects": [_project_info_to_dict(project) for project in projects],
    }

    if len(fresh_entries) > _PROJECT_LIST_CACHE_MAX_ENTRIES:
        ordered = sorted(
            fresh_entries.items(),
            key=lambda item: float(item[1].get("created_at", 0.0)),
            reverse=True,
        )
        fresh_entries = dict(ordered[:_PROJECT_LIST_CACHE_MAX_ENTRIES])

    payload["version"] = 1
    payload["entries"] = fresh_entries

    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        tmp = f"{cache_file}.tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, cache_file)
        try:
            os.chmod(cache_file, 0o600)
        except Exception:
            pass
    except Exception:
        return


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@click.command("list")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@click.option(
    "--all-workspaces",
    "all_workspaces",
    is_flag=True,
    default=True,
    help="Query all discovered workspaces (default, exhaustive).",
)
@pass_context
def list_projects_cmd(
    ctx: Context,
    json_output: bool,
    all_workspaces: bool,
) -> None:
    """List projects and their GPU quota.

    \b
    Examples:
        inspire project list          # Show project quota table
        inspire project list --json   # JSON output with all fields
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Listing projects requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    try:
        workspace_ids = session.all_workspace_ids
        if workspace_ids is None:
            # Workspace discovery never happened (stale session or login
            # method that doesn't support it).  Try using workspace IDs
            # from the config; if none are configured, fall back to the
            # session's single workspace_id.
            from inspire.config import Config as _Cfg

            try:
                _cfg, _ = _Cfg.from_files_and_env(
                    require_credentials=False, require_target_dir=False
                )
                cfg_candidates = [
                ]
                cfg_workspaces = getattr(_cfg, "workspaces", None)
                if isinstance(cfg_workspaces, dict):
                    cfg_candidates.extend(cfg_workspaces.values())
                cfg_ws = _unique_workspace_ids(cfg_candidates)
            except Exception:
                cfg_ws = []
            workspace_ids = cfg_ws or _unique_workspace_ids(
                [getattr(session, "workspace_id", None)]
            )
        else:
            workspace_ids = _unique_workspace_ids(list(workspace_ids))
        if not workspace_ids:
            # No discovered workspaces — only default query path applies.
            projects = _load_project_cache(
                session=session,
                mode="default-query",
                all_workspaces=False,
                workspace_ids=[],
            )
            if projects is None:
                projects = browser_api_module.list_projects(session=session)
                _save_project_cache(
                    session=session,
                    mode="default-query",
                    all_workspaces=False,
                    workspace_ids=[],
                    projects=projects,
                )
        elif not all_workspaces:
            # API-side reduction: prefer a single default project-list query
            # before probing per-workspace endpoints.
            projects = _load_project_cache(
                session=session,
                mode="default-query",
                all_workspaces=False,
                workspace_ids=[],
            )
            if projects is None:
                default_query_error: Exception | None = None
                try:
                    projects = browser_api_module.list_projects(session=session)
                except Exception as exc:
                    projects = []
                    default_query_error = exc

                if projects:
                    _save_project_cache(
                        session=session,
                        mode="default-query",
                        all_workspaces=False,
                        workspace_ids=[],
                        projects=projects,
                    )
                else:
                    query_workspace_ids = _select_workspace_ids_for_listing(
                        workspace_ids,
                        session_workspace_id=getattr(session, "workspace_id", None),
                        all_workspaces=False,
                    )
                    projects = _load_project_cache(
                        session=session,
                        mode="limited-fanout",
                        all_workspaces=False,
                        workspace_ids=query_workspace_ids,
                    )
                    if projects is None:
                        projects, workspace_errors = _collect_workspace_projects(
                            query_workspace_ids,
                            session=session,
                        )
                        if projects:
                            _save_project_cache(
                                session=session,
                                mode="limited-fanout",
                                all_workspaces=False,
                                workspace_ids=query_workspace_ids,
                                projects=projects,
                            )
                        elif workspace_errors and default_query_error is not None:
                            error_samples = ", ".join(
                                f"{ws_id}: {message}" for ws_id, message in workspace_errors[:3]
                            )
                            if len(workspace_errors) > 3:
                                error_samples += ", ..."
                            raise ValueError(
                                f"Failed to list projects across configured workspaces "
                                f"({len(workspace_errors)} failed: {error_samples}); "
                                f"default query failed: {default_query_error}"
                            ) from default_query_error
        else:
            query_workspace_ids = _select_workspace_ids_for_listing(
                workspace_ids,
                session_workspace_id=getattr(session, "workspace_id", None),
                all_workspaces=all_workspaces,
            )
            projects = _load_project_cache(
                session=session,
                mode="all-workspaces-fanout",
                all_workspaces=True,
                workspace_ids=query_workspace_ids,
            )
            if projects is None:
                projects, workspace_errors = _collect_workspace_projects(
                    query_workspace_ids,
                    session=session,
                )
                if not projects and workspace_errors:
                    try:
                        projects = browser_api_module.list_projects(session=session)
                    except Exception as e:
                        error_samples = ", ".join(
                            f"{ws_id}: {message}" for ws_id, message in workspace_errors[:3]
                        )
                        if len(workspace_errors) > 3:
                            error_samples += ", ..."
                        raise ValueError(
                            f"Failed to list projects across configured workspaces "
                            f"({len(workspace_errors)} failed: {error_samples}); "
                            f"default query failed: {e}"
                        ) from e
                _save_project_cache(
                    session=session,
                    mode="all-workspaces-fanout",
                    all_workspaces=True,
                    workspace_ids=query_workspace_ids,
                    projects=projects,
                )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to list projects: {e}", EXIT_API_ERROR)
        return

    results = [_project_to_dict(p) for p in projects]

    if json_output:
        click.echo(json_formatter.format_json({"projects": results, "total": len(results)}))
        return

    click.echo(human_formatter.format_project_list(results))


@click.command("detail")
@click.argument("project_id")
@pass_context
def detail_project_cmd(ctx: Context, project_id: str) -> None:
    """Show detail for a single project (`GET /api/v1/project/{id}`)."""
    session = require_web_session(ctx, hint="inspire project detail requires a logged-in web session")
    try:
        data = browser_api_module.get_project_detail(project_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    if ctx.json_output:
        click.echo(json_formatter.format_json(data))
        return

    click.echo("Project")
    click.echo(f"  ID:            {data.get('id', project_id)}")
    click.echo(f"  Name:          {data.get('name') or data.get('en_name') or 'N/A'}")
    if data.get("en_name") and data.get("en_name") != data.get("name"):
        click.echo(f"  English name:  {data.get('en_name')}")
    if data.get("description"):
        click.echo(f"  Description:   {data.get('description')}")
    if data.get("budget"):
        click.echo(f"  Budget:        {data.get('budget')}")
    if data.get("children_budget"):
        click.echo(f"  Children bgt:  {data.get('children_budget')}")
    if data.get("priority_name"):
        click.echo(f"  Priority:      {data.get('priority_name')} ({data.get('priority_level', '?')})")
    if data.get("created_at"):
        click.echo(f"  Created:       {format_epoch(data.get('created_at'))}")
    owner = data.get("creator") if isinstance(data.get("creator"), dict) else None
    if owner:
        click.echo(f"  Creator:       {owner.get('name', owner.get('id', '?'))}")


@click.command("owners")
@pass_context
def owners_project_cmd(ctx: Context) -> None:
    """List candidate project owners (`GET /api/v1/project/owners`)."""
    session = require_web_session(ctx, hint="inspire project owners requires a logged-in web session")
    try:
        items = browser_api_module.list_project_owners(session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    if ctx.json_output:
        click.echo(json_formatter.format_json({"total": len(items), "items": items}))
        return

    if not items:
        click.echo("No project owners returned.")
        return

    click.echo(f"Project Owners ({len(items)})")
    for i, it in enumerate(items, 1):
        name = it.get("name") or it.get("id") or "?"
        login = (it.get("extra_info") or {}).get("login_name", "")
        extra = f" ({login})" if login else ""
        click.echo(f"  [{i}] {name}{extra}")
