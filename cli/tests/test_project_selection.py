"""Tests for project selection behavior."""

from __future__ import annotations

import pytest

from inspire.config import Config
from inspire.cli.commands.notebook import notebook_create_flow
from inspire.platform.web.browser_api.projects import ProjectInfo, select_project


def _project(
    project_id: str,
    name: str,
    *,
    gpu_limit: bool = False,
    member_gpu_limit: bool = False,
    member_remain_gpu_hours: float = 0.0,
    priority_name: str = "0",
) -> ProjectInfo:
    return ProjectInfo(
        project_id=project_id,
        name=name,
        workspace_id="ws-test",
        gpu_limit=gpu_limit,
        member_gpu_limit=member_gpu_limit,
        member_remain_gpu_hours=member_remain_gpu_hours,
        priority_name=priority_name,
    )


# ---------------------------------------------------------------------------
# has_quota() — always True (scheduler enforces limits, not the CLI)
# ---------------------------------------------------------------------------


def test_has_quota_always_true_even_with_gpu_limit() -> None:
    """has_quota() returns True regardless of gpu_limit — CLI doesn't filter."""
    proj = _project(
        "p1",
        "Test",
        gpu_limit=True,
        member_gpu_limit=True,
        member_remain_gpu_hours=-9520985.6,
        priority_name="10",
    )
    assert proj.has_quota(needs_gpu=True) is True


def test_has_quota_true_without_gpu_limit() -> None:
    proj = _project("p1", "Test", member_gpu_limit=False, member_remain_gpu_hours=0.0)
    assert proj.has_quota(needs_gpu=True) is True


def test_has_quota_true_for_cpu_only() -> None:
    proj = _project(
        "p1",
        "Test",
        member_gpu_limit=True,
        member_remain_gpu_hours=-100.0,
    )
    assert proj.has_quota(needs_gpu=False) is True


# ---------------------------------------------------------------------------
# Auto-selection (no --project): project_order first, then gpu_unlimited, then priority
# ---------------------------------------------------------------------------


def test_auto_select_prefers_high_priority_among_unlimited() -> None:
    """Among unlimited projects, higher priority wins."""
    high_negative = _project(
        "p-high",
        "CI-高优先级",
        member_gpu_limit=True,
        member_remain_gpu_hours=-490226.4,
        priority_name="10",  # HIGH
    )
    low_huge = _project(
        "p-low",
        "项目兜底任务",
        member_gpu_limit=True,
        member_remain_gpu_hours=2399383493.5,
        priority_name="2",  # LOW
    )
    normal_zero = _project(
        "p-normal",
        "分布式测试",
        member_gpu_limit=False,
        member_remain_gpu_hours=0.0,
        priority_name="4",  # NORMAL
    )

    selected, message = select_project([low_huge, normal_zero, high_negative])

    assert selected.project_id == "p-high"
    assert message is None


def test_auto_select_prefers_unlimited_over_capped() -> None:
    """gpu_limit=False (unlimited) should be preferred over gpu_limit=True (capped)."""
    capped = _project(
        "p-capped",
        "Capped Project",
        gpu_limit=True,
        priority_name="10",
    )
    unlimited = _project(
        "p-unlimited",
        "Unlimited Project",
        gpu_limit=False,
        priority_name="4",  # lower priority, but unlimited wins
    )

    selected, _ = select_project([capped, unlimited])
    assert selected.project_id == "p-unlimited"


def test_auto_select_breaks_tie_by_name() -> None:
    """Same priority and hours → alphabetical name."""
    a = _project("p-a", "Alpha", priority_name="10")
    b = _project("p-b", "Beta", priority_name="10")

    selected, _ = select_project([b, a])
    assert selected.project_id == "p-a"


def test_auto_select_negative_gpu_hours_selectable() -> None:
    """Projects with negative member_remain_gpu_hours are still selectable."""
    proj = _project(
        "p1",
        "Only Project",
        member_gpu_limit=True,
        member_remain_gpu_hours=-186350.6,
        priority_name="10",
    )

    selected, message = select_project([proj])

    assert selected.project_id == "p1"
    assert message is None


# ---------------------------------------------------------------------------
# Explicit --project: always returns the requested project
# ---------------------------------------------------------------------------


def test_select_project_requested_returns_directly() -> None:
    """Requested project is returned directly — no fallback needed."""
    requested = _project(
        "project-requested",
        "Requested Project",
        member_gpu_limit=True,
        member_remain_gpu_hours=-10.0,
        priority_name="10",
    )
    other = _project(
        "project-other",
        "Other Project",
        member_gpu_limit=False,
        member_remain_gpu_hours=0.0,
        priority_name="4",
    )

    selected, message = select_project(
        [requested, other],
        requested="project-requested",
    )

    assert selected.project_id == "project-requested"
    assert message is None


def test_select_project_requested_over_quota_allowed_for_cpu() -> None:
    requested = _project(
        "project-requested",
        "Requested Project",
        member_gpu_limit=True,
        member_remain_gpu_hours=-10.0,
        priority_name="10",
    )
    fallback = _project(
        "project-fallback",
        "Fallback Project",
        member_gpu_limit=False,
        member_remain_gpu_hours=0.0,
        priority_name="4",
    )

    selected, message = select_project(
        [requested, fallback],
        requested="project-requested",
        needs_gpu_quota=False,
    )

    assert selected.project_id == "project-requested"
    assert message is None


def test_select_project_requested_not_found_raises() -> None:
    proj = _project("p1", "Exists", priority_name="4")

    with pytest.raises(ValueError, match="not found"):
        select_project([proj], requested="nonexistent")


# ---------------------------------------------------------------------------
# resolve_notebook_project integration
# ---------------------------------------------------------------------------


def test_resolve_notebook_project_passes_quota_and_shared_path_settings(monkeypatch) -> None:
    requested = _project(
        "project-requested",
        "Requested Project",
        member_gpu_limit=True,
        member_remain_gpu_hours=-10.0,
        priority_name="10",
    )
    called: dict[str, object] = {}

    def fake_select_project(
        projects,
        requested_value=None,
        *,
        allow_requested_over_quota=False,
        shared_path_group_by_id=None,
        needs_gpu_quota=True,
        project_order=None,
        congested_projects=None,
    ):
        called["requested"] = requested_value
        called["allow_requested_over_quota"] = allow_requested_over_quota
        called["shared_path_group_by_id"] = shared_path_group_by_id
        called["needs_gpu_quota"] = needs_gpu_quota
        called["project_order"] = project_order
        called["congested_projects"] = congested_projects
        return requested, None

    monkeypatch.setattr(
        notebook_create_flow.browser_api_module, "select_project", fake_select_project
    )

    config = Config(username="user", password="pass")

    resolved = notebook_create_flow.resolve_notebook_project(
        notebook_create_flow.Context(),
        projects=[requested],
        config=config,
        project="project-requested",
        allow_requested_over_quota=True,
        needs_gpu_quota=False,
        json_output=True,
    )

    assert resolved is requested
    assert called["requested"] == "project-requested"
    assert called["allow_requested_over_quota"] is True
    assert called["shared_path_group_by_id"] is None
    assert called["needs_gpu_quota"] is False


# ---------------------------------------------------------------------------
# project_order: user-defined selection order
# ---------------------------------------------------------------------------


def test_project_order_overrides_priority() -> None:
    """User-defined project_order should override priority-based selection."""
    high = _project("p-high", "HighPri", priority_name="10")
    low = _project("p-low", "LowPri", priority_name="2")

    # Without project_order: high priority wins
    selected, _ = select_project([high, low])
    assert selected.project_id == "p-high"

    # With project_order: low priority wins because user listed it first
    selected, _ = select_project([high, low], project_order=["LowPri", "HighPri"])
    assert selected.project_id == "p-low"


def test_project_order_cpu_mode_ignores_gpu_limit_preference() -> None:
    """In CPU mode, project_order should not be overridden by gpu_limit preference."""
    capped = _project("p-capped", "Capped", gpu_limit=True, priority_name="1")
    unlimited = _project("p-unlimited", "Unlimited", gpu_limit=False, priority_name="10")

    selected, _ = select_project(
        [capped, unlimited],
        needs_gpu_quota=False,
        project_order=["Capped", "Unlimited"],
    )
    assert selected.project_id == "p-capped"


def test_project_order_by_project_id() -> None:
    """project_order can match by project_id."""
    a = _project("p-aaa", "Alpha", priority_name="10")
    b = _project("p-bbb", "Beta", priority_name="4")

    selected, _ = select_project([a, b], project_order=["p-bbb"])
    assert selected.project_id == "p-bbb"


def test_project_order_case_insensitive_name() -> None:
    """project_order name matching should be case-insensitive."""
    a = _project("p-a", "Alpha", priority_name="10")
    b = _project("p-b", "Beta", priority_name="4")

    selected, _ = select_project([a, b], project_order=["beta"])
    assert selected.project_id == "p-b"


def test_project_order_unlisted_projects_fall_through() -> None:
    """Projects not in project_order sort after listed ones by priority."""
    listed = _project("p-listed", "Listed", priority_name="2")
    unlisted_high = _project("p-unlisted", "Unlisted", priority_name="10")

    selected, _ = select_project([unlisted_high, listed], project_order=["Listed"])
    assert selected.project_id == "p-listed"


def test_project_order_overrides_gpu_unlimited() -> None:
    """User-defined project_order should take priority over gpu_unlimited.

    Real scenario: CI has unlimited GPU hours but limited concurrent GPUs.
    User prefers CQ (capped) over CI (unlimited) — project_order must win.
    """
    capped = _project("p-capped", "CQ", gpu_limit=True, priority_name="6")
    unlimited = _project("p-unlimited", "CI", gpu_limit=False, priority_name="6")

    # Without project_order: unlimited wins (tiebreaker)
    selected, _ = select_project([capped, unlimited])
    assert selected.project_id == "p-unlimited"

    # With project_order: capped wins because user listed it first
    selected, _ = select_project(
        [capped, unlimited],
        project_order=["CQ", "CI"],
    )
    assert selected.project_id == "p-capped"


def test_project_order_empty_list_uses_default_sort() -> None:
    """Empty project_order should behave like no project_order."""
    high = _project("p-high", "HighPri", priority_name="10")
    low = _project("p-low", "LowPri", priority_name="2")

    selected, _ = select_project([high, low], project_order=[])
    assert selected.project_id == "p-high"


def test_project_order_does_not_affect_explicit_request() -> None:
    """Explicit --project should ignore project_order."""
    a = _project("p-a", "Alpha", priority_name="10")
    b = _project("p-b", "Beta", priority_name="4")

    selected, _ = select_project([a, b], requested="p-a", project_order=["Beta"])
    assert selected.project_id == "p-a"


# ---------------------------------------------------------------------------
# Congested projects: scheduling health-aware selection
# ---------------------------------------------------------------------------


def test_congested_project_strictly_filtered() -> None:
    """Auto-select skips congested project even if first in project_order."""
    first = _project("p-first", "First", priority_name="10")
    second = _project("p-second", "Second", priority_name="4")

    # Without congestion: first wins (higher priority)
    selected, _ = select_project(
        [first, second],
        project_order=["First", "Second"],
    )
    assert selected.project_id == "p-first"

    # With congestion on first: second is selected
    selected, _ = select_project(
        [first, second],
        project_order=["First", "Second"],
        congested_projects={"p-first"},
    )
    assert selected.project_id == "p-second"


def test_congested_project_explicit_still_selected_with_warning() -> None:
    """Explicit --project still selects congested project but returns warning."""
    proj = _project("p-congested", "Congested", priority_name="10")
    other = _project("p-other", "Other", priority_name="4")

    selected, msg = select_project(
        [proj, other],
        requested="p-congested",
        congested_projects={"p-congested"},
    )
    assert selected.project_id == "p-congested"
    assert msg is not None
    assert "Unschedulable" in msg
    assert "Congested" in msg


def test_no_congestion_data_uses_default_sort() -> None:
    """congested_projects=None doesn't change behavior."""
    high = _project("p-high", "HighPri", priority_name="10")
    low = _project("p-low", "LowPri", priority_name="2")

    selected, _ = select_project(
        [high, low],
        congested_projects=None,
    )
    assert selected.project_id == "p-high"


def test_all_projects_congested_falls_back() -> None:
    """When all projects are congested, still selects (no raise), uses normal sort."""
    high = _project("p-high", "HighPri", priority_name="10")
    low = _project("p-low", "LowPri", priority_name="2")

    selected, _ = select_project(
        [high, low],
        congested_projects={"p-high", "p-low"},
    )
    # Falls back to full list, high priority wins
    assert selected.project_id == "p-high"


def test_cpu_notebook_skips_health_check(monkeypatch) -> None:
    """CPU notebook (needs_gpu_quota=False) path doesn't trigger congestion logic."""
    proj = _project("p-a", "Alpha", priority_name="10")
    called: dict[str, object] = {}

    def fake_select_project(
        projects,
        requested_value=None,
        *,
        allow_requested_over_quota=False,
        shared_path_group_by_id=None,
        needs_gpu_quota=True,
        project_order=None,
        congested_projects=None,
    ):
        called["congested_projects"] = congested_projects
        return proj, None

    health_check_called = False

    def fake_check_scheduling_health(workspace_id, project_ids, session):
        nonlocal health_check_called
        health_check_called = True
        return {"p-a"}

    monkeypatch.setattr(
        notebook_create_flow.browser_api_module,
        "select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        notebook_create_flow.browser_api_module,
        "check_scheduling_health",
        fake_check_scheduling_health,
    )

    config = Config(username="user", password="pass")

    resolved = notebook_create_flow.resolve_notebook_project(
        notebook_create_flow.Context(),
        projects=[proj],
        config=config,
        project=None,
        allow_requested_over_quota=False,
        needs_gpu_quota=False,
        json_output=True,
        workspace_id="ws-test",
        session=object(),
    )

    assert resolved is proj
    assert not health_check_called
    assert called["congested_projects"] is None
