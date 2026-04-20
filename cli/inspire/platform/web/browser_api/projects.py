"""Browser (web-session) APIs for projects.

Projects are required for both training jobs and notebooks. The web UI exposes a
project listing endpoint with quota information that is not part of the OpenAPI
surface; this module contains the SSO-only implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
from inspire.platform.web.browser_api.jobs import list_job_events, list_jobs
from inspire.platform.web.session import DEFAULT_WORKSPACE_ID, WebSession, get_web_session

__all__ = [
    "ProjectInfo",
    "check_scheduling_health",
    "get_project_detail",
    "list_project_owners",
    "list_projects",
    "select_project",
]


@dataclass
class ProjectInfo:
    """Project information with quota details."""

    project_id: str
    name: str
    workspace_id: str
    # Quota fields
    budget: float = 0.0  # Total budget allocated
    remain_budget: float = 0.0  # Remaining budget
    member_remain_budget: float = 0.0  # Remaining budget for current user
    member_remain_gpu_hours: float = 0.0  # Member-level remaining GPU hours (informational)
    gpu_limit: bool = False  # Whether project-level GPU-hour limits are enforced
    member_gpu_limit: bool = False  # Whether member GPU limits are enforced (informational)
    priority_level: str = ""  # Priority level (HIGH, NORMAL, etc.)
    priority_name: str = ""  # Priority name (numeric string like "10", "4")

    @property
    def gpu_unlimited(self) -> bool:
        """True when the project has no project-level GPU-hour cap.

        Projects with ``gpu_limit=False`` never block job scheduling
        regardless of ``member_remain_gpu_hours``.  Projects with
        ``gpu_limit=True`` may queue indefinitely when their cumulative
        GPU-hour budget is exhausted.
        """
        return not self.gpu_limit

    def has_quota(self, *, needs_gpu: bool = True) -> bool:
        """Check if the project is safe to submit GPU work to.

        Returns ``True`` for projects without a GPU-hour cap
        (``gpu_limit=False``).  For capped projects (``gpu_limit=True``)
        we cannot reliably determine remaining quota from the API, so
        this also returns ``True`` — the scheduler will queue the job if
        the cap is hit.  Use :attr:`gpu_unlimited` to prefer uncapped
        projects in sorting.
        """
        return True

    def get_quota_status(self, *, needs_gpu: bool = True) -> str:
        """Get formatted quota status string for display."""
        if not needs_gpu:
            return ""
        if not self.gpu_limit:
            return " (no GPU-hour limit)"
        return " (GPU-hour limit enforced)"


def list_projects(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[ProjectInfo]:
    """List available projects."""
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body = {
        "page": 1,
        "page_size": -1,
        "filter": {
            "workspace_id": workspace_id,
            "check_admin": True,
        },
    }

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/project/list"),
        referer=f"{_get_base_url()}/jobs/interactiveModeling",
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    items = data.get("data", {}).get("items", [])

    def _parse_float(value) -> float:
        if value is None or value == "":
            return 0.0
        try:
            return float(value)
        except (ValueError, TypeError):
            return 0.0

    return [
        ProjectInfo(
            project_id=item.get("id", ""),
            name=item.get("name", ""),
            workspace_id=item.get("workspace_id", workspace_id),
            budget=_parse_float(item.get("budget")),
            remain_budget=_parse_float(item.get("remain_budget")),
            member_remain_budget=_parse_float(item.get("member_remain_budget")),
            member_remain_gpu_hours=_parse_float(item.get("member_remain_gpu_hours")),
            gpu_limit=bool(item.get("gpu_limit", False)),
            member_gpu_limit=bool(item.get("member_gpu_limit", False)),
            priority_level=item.get("priority_level", ""),
            priority_name=item.get("priority_name", ""),
        )
        for item in items
    ]


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
    allow_requested_over_quota: bool = False,
    shared_path_group_by_id: dict[str, str] | None = None,
    needs_gpu_quota: bool = True,
    project_order: list[str] | None = None,
    congested_projects: set[str] | None = None,
) -> tuple[ProjectInfo, Optional[str]]:
    """Select a project, with auto-fallback if over quota.

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
        Matching is case-insensitive on name, and exact on project_id.
        """
        if not project_order:
            return 0  # no preference — all equal
        for i, entry in enumerate(project_order):
            if project.name.lower() == entry.lower() or project.project_id == entry:
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

    def _quota_candidates(items: list[ProjectInfo]) -> list[ProjectInfo]:
        return [p for p in items if p.has_quota(needs_gpu=needs_gpu_quota)]

    def _best_by_quota(items: list[ProjectInfo]) -> ProjectInfo | None:
        if not items:
            return None
        return sorted(items, key=_sort_key)[0]

    def _format_candidates(items: list[ProjectInfo]) -> str:
        ordered = sorted(
            items,
            key=lambda p: (
                not p.has_quota(needs_gpu=needs_gpu_quota),
                _sort_key(p),
            ),
        )
        lines = [
            "Candidates:",
            *(
                f"  - {p.name} ({p.project_id}){p.get_quota_status(needs_gpu=needs_gpu_quota)}"
                for p in ordered
                if p.name
            ),
        ]
        return "\n".join(lines)

    if requested:
        target = None
        for project in projects:
            if project.name.lower() == requested.lower() or project.project_id == requested:
                target = project
                break

        if not target:
            raise ValueError(f"Project '{requested}' not found")

        if target.has_quota(needs_gpu=needs_gpu_quota):
            msg = None
            if congested_projects and target.project_id in congested_projects:
                msg = (
                    f"Warning: project '{target.name}' has jobs stuck as Unschedulable "
                    "— GPUs may not be available."
                )
            return (target, msg)

        if allow_requested_over_quota:
            proceed_msg = (
                f"Project '{target.name}' is over quota, but continuing with the explicitly "
                "requested project."
            )
            return (target, proceed_msg)

        fallback_candidates = [
            p for p in projects if p is not target and p.has_quota(needs_gpu=needs_gpu_quota)
        ]

        target_group = None
        if shared_path_group_by_id is not None:
            target_group = str(shared_path_group_by_id.get(target.project_id) or "").strip() or None

        compatible_candidates = fallback_candidates
        incompatible: list[ProjectInfo] = []
        if target_group and shared_path_group_by_id is not None:
            compatible_candidates = []
            for project in fallback_candidates:
                group = str(shared_path_group_by_id.get(project.project_id) or "").strip()
                if group and group != target_group:
                    incompatible.append(project)
                    continue
                compatible_candidates.append(project)

        fallback = _best_by_quota(compatible_candidates)
        if fallback is None:
            suffix = ""
            if target_group and incompatible:
                suffix = (
                    "\n\nNote: Some in-quota projects were excluded due to shared-path mismatch "
                    f"(target group: {target_group})."
                )
            raise ValueError(
                "All compatible projects are over quota\n" + _format_candidates(projects) + suffix
            )

        group_note = ""
        if target_group and shared_path_group_by_id is not None:
            fallback_group = str(shared_path_group_by_id.get(fallback.project_id) or "").strip()
            if not fallback_group:
                group_note = (
                    " Warning: selected fallback project has unknown shared-path group; "
                    "run 'inspire init --discover --probe-shared-path' to populate it."
                )

        fallback_msg = (
            f"Project '{target.name}' is over quota; using '{fallback.name}'. "
            "Hint: pass --project <name-or-id> to override."
        )
        if group_note:
            fallback_msg = fallback_msg + group_note
        return (fallback, fallback_msg)

    candidates = _quota_candidates(projects)
    if congested_projects:
        healthy = [p for p in candidates if p.project_id not in congested_projects]
        if healthy:
            candidates = healthy

    selected = _best_by_quota(candidates)
    if selected is None:
        raise ValueError("All projects are over quota\n" + _format_candidates(projects))

    return (selected, None)


# ---------------------------------------------------------------------------
# Detail + owners (Browser-API only; not covered by OpenAPI)
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
