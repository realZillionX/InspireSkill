"""Browser (web-session) APIs for projects.

Projects are required for both training jobs and notebooks. The web UI exposes
project listing endpoints with quota information through the web-session API;
this module contains the SSO-only implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
from inspire.platform.web.browser_api.jobs import list_job_events, list_jobs
from inspire.platform.web.session import WebSession, get_web_session

_PROJECT_LIST_PAGE_SIZE = 100

__all__ = [
    "ProjectInfo",
    "check_scheduling_health",
    "get_project_detail",
    "list_all_projects",
    "list_project_owners",
    "list_project_page_records",
    "list_projects",
    "list_projects_v2",
    "select_project",
]


@dataclass
class ProjectInfo:
    """Project information with quota details."""

    project_id: str
    name: str
    workspace_id: str
    en_name: str = ""
    # Quota fields
    member_remain_budget: float = 0.0  # Remaining budget for current user
    gpu_limit: bool = False  # Whether project-level GPU-hour limits are enforced
    priority_level: str = ""  # Priority level (HIGH, NORMAL, etc.)
    priority_name: str = ""  # Priority name (numeric string like "10", "4")
    workspace_ids: tuple[str, ...] = ()
    workspace_names: tuple[str, ...] = ()

    @property
    def gpu_unlimited(self) -> bool:
        """True when the project has no project-level GPU-hour cap.

        Projects with ``gpu_limit=False`` never block job scheduling.
        Projects with ``gpu_limit=True`` may queue indefinitely when their
        cumulative GPU-hour budget is exhausted.
        """
        return not self.gpu_limit

    def get_quota_status(self, *, needs_gpu: bool = True) -> str:
        """Get formatted quota status string for display."""
        if not needs_gpu:
            return ""
        if not self.gpu_limit:
            return " (no GPU-hour limit)"
        return " (GPU-hour limit enforced)"


def _parse_float(value) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _project_info_from_item(item: dict[str, Any], *, workspace_id: str = "") -> ProjectInfo:
    space_list = item.get("space_list")
    workspace_ids: list[str] = []
    workspace_names: list[str] = []
    if isinstance(space_list, list):
        for space in space_list:
            if not isinstance(space, dict):
                continue
            sid = str(space.get("id") or space.get("workspace_id") or "").strip()
            if sid and sid not in workspace_ids:
                workspace_ids.append(sid)
            name = str(space.get("name") or space.get("workspace_name") or "").strip()
            if name and name not in workspace_names:
                workspace_names.append(name)

    resolved_workspace_id = (
        str(item.get("workspace_id") or "").strip()
        or workspace_id
        or (workspace_ids[0] if workspace_ids else "")
    )
    if resolved_workspace_id and resolved_workspace_id not in workspace_ids:
        workspace_ids.insert(0, resolved_workspace_id)

    remain_budget = _parse_float(item.get("remain_budget"))
    member_remain_budget = _parse_float(item.get("member_remain_budget"))
    if member_remain_budget == 0.0 and "member_remain_budget" not in item:
        member_remain_budget = remain_budget

    return ProjectInfo(
        project_id=item.get("id", ""),
        name=item.get("name", ""),
        workspace_id=resolved_workspace_id,
        en_name=item.get("en_name", ""),
        member_remain_budget=member_remain_budget,
        gpu_limit=bool(item.get("gpu_limit", False)),
        priority_level=item.get("priority_level", ""),
        priority_name=item.get("priority_name", ""),
        workspace_ids=tuple(workspace_ids),
        workspace_names=tuple(workspace_names),
    )


def _coerce_total(value: Any, fallback: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return fallback


def _list_project_items(
    *,
    session: WebSession,
    filter_body: dict[str, Any],
    referer: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1

    while True:
        body = {
            "page": page,
            "page_size": _PROJECT_LIST_PAGE_SIZE,
            "filter": dict(filter_body),
        }
        data = _request_json(
            session,
            "POST",
            _browser_api_path("/project/list"),
            referer=referer,
            body=body,
            timeout=30,
        )

        if data.get("code") != 0:
            raise ValueError(f"API error: {data.get('message')}")

        payload = data.get("data", {}) or {}
        page_items = payload.get("items", [])
        if not isinstance(page_items, list):
            page_items = []
        items.extend(item for item in page_items if isinstance(item, dict))

        total = _coerce_total(payload.get("total"), len(items))
        if len(items) >= total or len(page_items) < _PROJECT_LIST_PAGE_SIZE:
            return items
        page += 1


def list_projects(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[ProjectInfo]:
    """List available projects."""
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        raise ValueError("Workspace selection is required.")

    items = _list_project_items(
        session=session,
        filter_body={
            "workspace_id": workspace_id,
            "check_admin": True,
        },
        referer=f"{_get_base_url()}/jobs/interactiveModeling",
    )
    return [
        _project_info_from_item(item, workspace_id=workspace_id)
        for item in items
        if isinstance(item, dict)
    ]


def list_all_projects(session: Optional[WebSession] = None) -> list[ProjectInfo]:
    """List all visible projects with one project-scoped browser API call."""
    if session is None:
        session = get_web_session()

    items = _list_project_items(
        session=session,
        filter_body={
            "check_admin": True,
        },
        referer=f"{_get_base_url()}/projects",
    )
    return [
        _project_info_from_item(item)
        for item in items
        if isinstance(item, dict)
    ]


def list_projects_v2(
    workspace_id: Optional[str] = None,
    *,
    check_admin: bool | None = True,
    page: int = 1,
    page_size: int = -1,
    session: Optional[WebSession] = None,
) -> tuple[list[dict[str, Any]], int]:
    """List projects from the current frontend selector endpoint.

    Endpoint: ``POST /api/v1/project/list_v2``. The UI uses this endpoint for
    project drop-downs in notebook / train / model / serving forms.
    """
    if session is None:
        session = get_web_session()
    if workspace_id is None:
        raise ValueError("Workspace selection is required.")

    filter_body: dict[str, Any] = {"workspace_id": workspace_id}
    if check_admin is not None:
        filter_body["check_admin"] = check_admin
    body = {"filter": filter_body, "page": page, "page_size": page_size}

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/project/list_v2"),
        referer=f"{_get_base_url()}/jobs/interactiveModeling",
        body=body,
        timeout=30,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    payload = data.get("data") or {}
    items = payload.get("items")
    if not isinstance(items, list):
        items = []
    total_raw = payload.get("total")
    try:
        total = int(str(total_raw)) if total_raw is not None else len(items)
    except ValueError:
        total = len(items)
    return [item for item in items if isinstance(item, dict)], total


def list_project_page_records(
    *,
    page: int = 1,
    page_size: int = 10,
    filter_body: Optional[dict[str, Any]] = None,
    session: Optional[WebSession] = None,
) -> tuple[list[dict[str, Any]], int]:
    """List project-management page records via ``POST /api/v1/project/list_for_page``."""
    if session is None:
        session = get_web_session()
    body = {"page": page, "page_size": page_size, "filter": dict(filter_body or {})}
    data = _request_json(
        session,
        "POST",
        _browser_api_path("/project/list_for_page"),
        referer=f"{_get_base_url()}/projects",
        body=body,
        timeout=30,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    payload = data.get("data") or {}
    items = payload.get("items")
    if not isinstance(items, list):
        items = []
    total_raw = payload.get("total")
    try:
        total = int(str(total_raw)) if total_raw is not None else len(items)
    except ValueError:
        total = len(items)
    return [item for item in items if isinstance(item, dict)], total


def check_scheduling_health(
    workspace_id: str,
    project_ids: set[str],
    session: WebSession,
) -> set[str]:
    """Return project_ids that have Unschedulable queuing jobs.

    Fully best-effort: returns empty set on any API failure.
    """
    try:
        jobs, _ = list_jobs(
            workspace_id=workspace_id,
            status="job_queuing",
            page_size=50,
            session=session,
        )
    except Exception:
        return set()

    # Group queuing jobs by project_id, keeping only projects we care about.
    project_jobs: dict[str, list[str]] = {}
    for job in jobs:
        pid = job.project_id
        if pid in project_ids:
            project_jobs.setdefault(pid, []).append(job.job_id)

    congested: set[str] = set()
    for pid, job_ids in project_jobs.items():
        try:
            events = list_job_events(job_ids[0], session=session)
            if any(e.get("reason") == "Unschedulable" for e in events):
                congested.add(pid)
        except Exception:
            continue

    return congested


def select_project(
    projects: list[ProjectInfo],
    requested: Optional[str] = None,
    *,
    needs_gpu_quota: bool = True,
    project_order: list[str] | None = None,
    congested_projects: set[str] | None = None,
) -> tuple[ProjectInfo, Optional[str]]:
    """Select a project by explicit name or scheduling preference.

    Sorting priority (when auto-selecting):
      - GPU workloads (``needs_gpu_quota=True``):
        1. ``congested_projects`` — strictly filter out projects with Unschedulable jobs
        2. ``project_order`` — user-defined preference ranking
        3. ``gpu_unlimited`` — prefer uncapped projects (tiebreaker)
        4. ``priority_name`` — higher numeric priority first
        5. alphabetical name
      - CPU workloads (``needs_gpu_quota=False``):
        1. ``project_order`` — user-defined preference ranking
        2. ``priority_name`` — higher numeric priority first
        3. alphabetical name
    """

    def _priority_value(project: ProjectInfo) -> int:
        try:
            return int(project.priority_name) if project.priority_name else 0
        except ValueError:
            return 0

    def _order_rank(project: ProjectInfo) -> int:
        """Return position in user-defined project_order (lower is better).

        Projects not in the list get a large rank so they sort after listed ones.
        Matching is case-insensitive on name only.
        """
        if not project_order:
            return 0  # no preference — all equal
        for i, entry in enumerate(project_order):
            if project.name.lower() == entry.lower():
                return i
        return len(project_order)  # unlisted → after all listed

    def _gpu_cap_rank(project: ProjectInfo) -> int:
        # Only prefer uncapped projects for GPU workloads.
        if not needs_gpu_quota:
            return 0
        return 0 if project.gpu_unlimited else 1

    def _sort_key(project: ProjectInfo) -> tuple:
        return (
            _order_rank(project),
            _gpu_cap_rank(project),
            -_priority_value(project),
            project.name.lower(),
        )

    def _best_project(items: list[ProjectInfo]) -> ProjectInfo | None:
        if not items:
            return None
        return sorted(items, key=_sort_key)[0]

    if requested:
        target = None
        for project in projects:
            if project.name.lower() == requested.lower():
                target = project
                break

        if not target:
            raise ValueError("Project name not found")

        msg = None
        if congested_projects and target.project_id in congested_projects:
            msg = (
                f"Warning: project '{target.name}' has jobs stuck as Unschedulable "
                "— GPUs may not be available."
            )
        return (target, msg)

    candidates = projects
    if congested_projects:
        healthy = [p for p in candidates if p.project_id not in congested_projects]
        if healthy:
            candidates = healthy

    selected = _best_project(candidates)
    if selected is None:
        raise ValueError("No projects available")

    return (selected, None)


# ---------------------------------------------------------------------------
# Detail + owners (Browser API project management views)
# ---------------------------------------------------------------------------


def get_project_detail(
    project_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Fetch a project's detail via `GET /api/v1/project/{project_id}`.

    Returns the raw `data` dict: budget / children_budget / created_at /
    en_name / description / priority / owner metadata. CLI-facing code should
    tolerate the shape since fields are platform-defined and may drift.
    """
    if session is None:
        session = get_web_session()
    data = _request_json(
        session,
        "GET",
        _browser_api_path(f"/project/{project_id}"),
        referer=f"{_get_base_url()}/projects",
        timeout=15,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")
    return data.get("data") or {}


def list_project_owners(session: Optional[WebSession] = None) -> list[dict]:
    """List candidate project owners (`GET /api/v1/project/owners`).

    Backs the "负责人" dropdown when creating a job. Returns the raw `items`
    array; each entry typically carries `{id, name, login_name, ...}`.
    """
    if session is None:
        session = get_web_session()
    data = _request_json(
        session,
        "GET",
        _browser_api_path("/project/owners"),
        referer=f"{_get_base_url()}/projects",
        timeout=15,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")
    items = (data.get("data") or {}).get("items")
    return items if isinstance(items, list) else []
