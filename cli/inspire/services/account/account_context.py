"""Live account context collection and output budgeting."""

from __future__ import annotations

from typing import Any, Callable

from inspire.config import Config
from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.services.utils.collections import bound_collection


_CONTEXT_COLLECTION_KEYS = (
    "projects",
    "workspaces",
    "compute_groups",
)


def collect_context(
    cfg: Config,
    *,
    session: Any = None,
    account: str | None = None,
    workspaces_loader: Callable[[], dict[str, str]] | None = None,
    projects_loader: Callable[[], list[Any]] | None = None,
    groups_loader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    from inspire.accounts import current_account
    from inspire.config.workspaces import workspace_name_map
    from inspire.platform.web import browser_api as browser_api_module
    from inspire.platform.web.session import get_web_session

    warnings: list[str] = []
    active_account = (
        scrub_raw_ids((account if account is not None else current_account()) or "") or None
    )

    # One live session feeds every catalog below. Account config deliberately
    # carries no project or compute-group snapshot, so falling back to Config
    # here would quietly reintroduce the stale-catalog bug that init removes.
    ws_name_for_id: dict[str, str] = {}
    try:
        session = session if session is not None else get_web_session()
        for workspace_id, raw_name in (
            workspaces_loader() if workspaces_loader else workspace_name_map(session)
        ).items():
            name = scrub_raw_ids(raw_name)
            if name:
                ws_name_for_id[workspace_id] = name
    except Exception:
        warnings.append("Workspace names are unavailable. Run `inspire account check` and retry.")
    workspaces_view = sorted(set(ws_name_for_id.values()))

    # Projects are global objects that can span workspaces, so use the one-call
    # live project listing instead of querying once per workspace.
    projects_view: list[dict[str, str]] = []
    if session is not None:
        try:
            project_names = {
                scrub_raw_ids(str(getattr(project, "name", "") or "").strip())
                for project in (
                    projects_loader()
                    if projects_loader
                    else browser_api_module.list_all_projects(session=session)
                )
            }
            projects_view = [{"name": name} for name in sorted(project_names) if name]
        except Exception:
            warnings.append("Project names are unavailable. Run `inspire account check` and retry.")

    # Compute groups are workspace-scoped. Preserve successful workspace rows
    # when one workspace fails, but say that the aggregate is incomplete.
    compute_groups_view: list[dict[str, Any]] = []
    if session is not None:
        group_workspaces: dict[str, set[str]] = {}
        failed_workspace_count = 0
        for workspace_id, workspace_name in sorted(
            ws_name_for_id.items(),
            key=lambda item: (item[1], item[0]),
        ):
            try:
                groups = (
                    groups_loader(workspace_id)
                    if groups_loader
                    else browser_api_module.list_compute_groups(
                        workspace_id=workspace_id,
                        session=session,
                    )
                )
            except Exception:
                failed_workspace_count += 1
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                raw_name = (
                    group.get("name")
                    or group.get("logic_compute_group_name")
                    or group.get("compute_group_name")
                    or ""
                )
                name = scrub_raw_ids(str(raw_name).strip())
                if name:
                    group_workspaces.setdefault(name, set()).add(workspace_name)

        for name, workspace_names_set in group_workspaces.items():
            workspace_names = sorted(workspace_names_set)
            entry: dict[str, Any] = {"name": name}
            if workspace_names:
                entry["workspace"] = (
                    workspace_names[0] if len(workspace_names) == 1 else workspace_names
                )
            compute_groups_view.append(entry)
        compute_groups_view.sort(
            key=lambda entry: (
                str(entry.get("workspace") or ""),
                str(entry["name"]),
            )
        )
        if failed_workspace_count:
            warnings.append(
                "Compute group names are incomplete: "
                f"{failed_workspace_count} workspace(s) could not be queried. "
                "Run `inspire account check` and retry."
            )

    data: dict[str, Any] = {
        "active": {
            "account": active_account,
        },
        "projects": projects_view,
        "workspaces": workspaces_view,
        "compute_groups": compute_groups_view,
    }
    if warnings:
        data["warnings"] = warnings
    return data


def bound_context(data: dict[str, Any], limit: int | None) -> dict[str, Any]:
    bounded: dict[str, Any] = {"active": data["active"]}
    truncation: dict[str, dict[str, int]] = {}

    for key in _CONTEXT_COLLECTION_KEYS:
        page = bound_collection(data.get(key) or [], limit=limit)
        bounded[key] = page.items
        if page.truncated:
            truncation[key] = {
                "shown": page.shown,
                "total": page.total,
            }

    if truncation:
        bounded["truncated"] = truncation
    warnings = data.get("warnings")
    if isinstance(warnings, list) and warnings:
        bounded["warnings"] = warnings
    return bounded
