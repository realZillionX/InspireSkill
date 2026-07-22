"""Wiring tests for workspace-aware priority at create boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from inspire.cli.commands import batch
from inspire.cli.commands.ray import ray_commands
from inspire.cli.context import Context
from inspire.cli.utils import quota_resolver
from inspire.cli.utils.quota_resolver import ResolvedQuota
from inspire.cli.utils.task_priority import resolve_workspace_task_priority
from inspire.config import Config
from inspire.platform.web.browser_api import projects, workspaces
from inspire.task_priority import TaskPriorityError

WORKSPACE_ID = "ws-11111111-1111-1111-1111-111111111111"
PROJECT_ID = "project-11111111-1111-1111-1111-111111111111"


def _config() -> Config:
    return Config(
        username="user",
        password="pass",
        projects={"project": PROJECT_ID},
    )


def _resolved_quota() -> ResolvedQuota:
    return ResolvedQuota(
        quota_id="quota-11111111-1111-1111-1111-111111111111",
        logic_compute_group_id="group-11111111-1111-1111-1111-111111111111",
        compute_group_name="group",
        gpu_count=0,
        cpu_count=4,
        memory_gib=16,
        gpu_type="",
        raw_price={},
    )


def _patch_fair_ray_boundary(
    monkeypatch: pytest.MonkeyPatch,
    *,
    project_limit: str,
) -> None:
    monkeypatch.setattr(ray_commands, "select_workspace_id", lambda *_args, **_kwargs: WORKSPACE_ID)
    monkeypatch.setattr(ray_commands, "_resolve_image_id", lambda *_args, **_kwargs: "image-id")
    monkeypatch.setattr(quota_resolver, "resolve_quota", lambda **_kwargs: _resolved_quota())
    monkeypatch.setattr(workspaces, "is_fair_scheduling_workspace", lambda *_args: True)
    monkeypatch.setattr(
        projects,
        "list_projects",
        lambda **_kwargs: [
            projects.ProjectInfo(
                project_id=PROJECT_ID,
                name="Project",
                workspace_id=WORKSPACE_ID,
                priority_name=project_limit,
            )
        ],
    )


def _assemble_ray_body(*, priority: int | None) -> dict[str, Any]:
    return ray_commands._assemble_create_body(
        Context(),
        config=_config(),
        session=object(),
        name="ray",
        command="python driver.py",
        description="",
        project="project",
        workspace="workspace",
        priority=priority,
        image="head-image",
        image_type="SOURCE_PUBLIC",
        group="group",
        quota="0,4,16",
        shm_size=None,
        workers=("name=worker;image=worker-image;group=group;" "quota=0,4,16;min=1;max=1",),
    )


@pytest.mark.parametrize(("project_limit", "expected"), [("10", 4), ("2", 1)])
def test_fair_ray_create_defaults_and_applies_project_cap(
    monkeypatch: pytest.MonkeyPatch,
    project_limit: str,
    expected: int,
) -> None:
    _patch_fair_ray_boundary(monkeypatch, project_limit=project_limit)

    assert _assemble_ray_body(priority=None)["task_priority"] == expected


def test_fair_ray_create_rejects_unavailable_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fair_ray_boundary(monkeypatch, project_limit="10")

    with pytest.raises(TaskPriorityError, match="only priority 1=LOW or 4=HIGH"):
        _assemble_ray_body(priority=2)


@pytest.mark.parametrize(("project_limit", "expected"), [("3", 1), ("10", 4)])
def test_workspace_resolver_loads_fair_project_limit_by_id(
    monkeypatch: pytest.MonkeyPatch,
    project_limit: str,
    expected: int,
) -> None:
    calls: list[tuple[str, object]] = []
    session = object()
    monkeypatch.setattr(workspaces, "is_fair_scheduling_workspace", lambda *_args: True)

    def _list_projects(*, workspace_id: str, session: object):
        calls.append((workspace_id, session))
        return [
            projects.ProjectInfo(
                project_id=PROJECT_ID,
                name="Project",
                workspace_id=WORKSPACE_ID,
                priority_name=project_limit,
            )
        ]

    monkeypatch.setattr(projects, "list_projects", _list_projects)

    assert (
        resolve_workspace_task_priority(
            None,
            session=session,
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT_ID,
        )
        == expected
    )
    assert calls == [(WORKSPACE_ID, session)]


def test_workspace_resolver_does_not_load_project_in_standard_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspaces, "is_fair_scheduling_workspace", lambda *_args: False)
    monkeypatch.setattr(
        projects,
        "list_projects",
        lambda **_kwargs: pytest.fail("standard priority must not query project policy"),
    )

    assert (
        resolve_workspace_task_priority(
            None,
            session=object(),
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT_ID,
        )
        == 10
    )


def _patch_fair_training_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(batch, "select_workspace_id", lambda *_args, **_kwargs: WORKSPACE_ID)
    monkeypatch.setattr(batch, "resolve_quota", lambda **_kwargs: _resolved_quota())
    monkeypatch.setattr(workspaces, "is_fair_scheduling_workspace", lambda *_args: True)
    monkeypatch.setattr(
        batch.job_submit,
        "select_project_for_workspace",
        lambda *_args, **_kwargs: (
            SimpleNamespace(
                project_id=PROJECT_ID,
                name="Project",
                priority_name="10",
            ),
            None,
        ),
    )
    monkeypatch.setattr(batch.job_submit, "build_training_job_plan", lambda **kwargs: kwargs)


def _training_batch_item(*, priority: int | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": "training",
        "command": "python train.py",
        "workspace": "workspace",
        "project": "project",
        "group": "group",
        "quota": "0,4,16",
        "image": "image",
    }
    if priority is not None:
        item["priority"] = priority
    return item


def test_fair_training_batch_defaults_to_high_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fair_training_batch(monkeypatch)

    plan = batch._prepare_training_item(
        _training_batch_item(),
        config=_config(),
        session=object(),
    )

    assert plan["priority"] == 4


def test_fair_training_batch_rejects_unavailable_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fair_training_batch(monkeypatch)

    with pytest.raises(TaskPriorityError, match="only priority 1=LOW or 4=HIGH"):
        batch._prepare_training_item(
            _training_batch_item(priority=2),
            config=_config(),
            session=object(),
        )
