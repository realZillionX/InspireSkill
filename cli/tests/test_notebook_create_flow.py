"""Tests for `run_notebook_create` orchestration (quota-based flow)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from inspire.cli.commands.notebook import notebook_create_flow as flow_module
from inspire.cli.context import Context
from inspire.cli.utils.quota_resolver import QuotaSpec, ResolvedQuota


def _make_resolved_quota(
    *,
    gpu_count: int = 1,
    cpu_count: int = 20,
    memory_gib: int = 200,
    gpu_type: str = "H200",
) -> ResolvedQuota:
    return ResolvedQuota(
        quota_id=f"quota-{gpu_type.lower()}" if gpu_count else "quota-cpu",
        logic_compute_group_id="lcg-test",
        compute_group_name=f"{gpu_type} Group" if gpu_count else "CPU Pool",
        gpu_count=gpu_count,
        cpu_count=cpu_count,
        memory_gib=memory_gib,
        gpu_type=gpu_type if gpu_count else "",
        raw_price={"cpu_info": {"cpu_type": "Test CPU"}},
    )


def test_format_quota_display_gpu() -> None:
    display = flow_module.format_quota_display(_make_resolved_quota())
    assert display == "1xH200 + 20CPU + 200GiB"


def test_format_quota_display_cpu_only() -> None:
    display = flow_module.format_quota_display(
        _make_resolved_quota(gpu_count=0, cpu_count=4, memory_gib=32, gpu_type="")
    )
    assert display == "4CPU + 32GiB"


def test_resolve_create_inputs_uses_config_quota_default() -> None:
    config = SimpleNamespace(
        notebook_quota="2,40,400",
        project_order=None,
        job_project_id="project-x",
        notebook_image=None,
        job_image="img-x",
        shm_size=64,
    )
    quota, project, image, shm = flow_module._resolve_create_inputs(
        config=config, quota=None, project=None, image=None, shm_size=None
    )
    assert quota == "2,40,400"
    assert project == "project-x"
    assert image == "img-x"
    assert shm == 64


def test_resolve_create_inputs_prefers_cli_arg_over_config() -> None:
    config = SimpleNamespace(
        notebook_quota="2,40,400",
        project_order=None,
        job_project_id=None,
        notebook_image=None,
        job_image=None,
        shm_size=None,
    )
    quota, _p, _i, shm = flow_module._resolve_create_inputs(
        config=config, quota="1,20,200", project=None, image=None, shm_size=None
    )
    assert quota == "1,20,200"
    assert shm == 32  # default fallback


def test_resolve_create_inputs_requires_quota_somewhere() -> None:
    config = SimpleNamespace(
        notebook_quota=None,
        project_order=None,
        job_project_id=None,
        notebook_image=None,
        job_image=None,
        shm_size=None,
    )
    with pytest.raises(ValueError, match="--quota is required"):
        flow_module._resolve_create_inputs(
            config=config, quota=None, project=None, image=None, shm_size=None
        )


def _configure_create_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wait_result: bool,
    post_start_value: str | None = "echo from config",
    resolved_quota: ResolvedQuota | None = None,
) -> tuple[Context, dict[str, object]]:
    ctx = Context()
    calls: dict[str, object] = {}
    resolved = resolved_quota or _make_resolved_quota()

    config = SimpleNamespace(
        notebook_quota="1,20,200",
        project_order=None,
        job_project_id="project-1111",
        notebook_image=None,
        notebook_post_start=post_start_value,
        job_image="img-default",
        shm_size=32,
        job_priority=9,
        projects={},
        project_shared_path_groups={},
        job_workspace_id="ws-1111",
        workspaces={"gpu": "ws-1111"},
    )

    selected_project = SimpleNamespace(
        project_id="project-1111",
        name="Project One",
        priority_name="6",
    )
    selected_image = SimpleNamespace(
        image_id="img-1111",
        url="docker://image",
        name="Image One",
    )

    monkeypatch.setattr(flow_module, "resolve_json_output", lambda _ctx, _json: False)
    monkeypatch.setattr(flow_module, "require_web_session", lambda _ctx, hint=None: object())
    monkeypatch.setattr(flow_module, "load_config", lambda _ctx: config)
    monkeypatch.setattr(
        flow_module,
        "_resolve_workspace_id",
        lambda _ctx, **_kwargs: "ws-1111",
    )

    def fake_resolve_quota(*, spec, workspace_id, session=None, **_):  # noqa: ANN001
        calls["resolve_quota_spec"] = spec
        return resolved

    monkeypatch.setattr(flow_module, "resolve_quota", fake_resolve_quota)
    monkeypatch.setattr(
        flow_module,
        "_fetch_workspace_projects",
        lambda *_args, **_kwargs: [selected_project],
    )
    monkeypatch.setattr(
        flow_module,
        "resolve_notebook_project",
        lambda *_args, **_kwargs: selected_project,
    )
    monkeypatch.setattr(
        flow_module,
        "_fetch_notebook_images",
        lambda *_args, **_kwargs: [selected_image],
    )
    monkeypatch.setattr(
        flow_module,
        "resolve_notebook_image",
        lambda *_args, **_kwargs: selected_image,
    )

    def fake_create_notebook_and_report(*_args, **kwargs):  # noqa: ANN001
        calls["task_priority"] = kwargs["task_priority"]
        calls["quota"] = kwargs["quota"]
        return "nb-1111"

    monkeypatch.setattr(flow_module, "create_notebook_and_report", fake_create_notebook_and_report)

    def fake_wait_for_running(*_args, **_kwargs):  # noqa: ANN001
        calls["wait_args"] = {
            "wait": _kwargs["wait"],
            "needs_post_start": _kwargs["needs_post_start"],
        }
        if _kwargs["wait"] or _kwargs["needs_post_start"]:
            calls["wait_called"] = True
        return wait_result

    monkeypatch.setattr(flow_module, "maybe_wait_for_running", fake_wait_for_running)

    def fake_post_start(*_args, **kwargs):  # noqa: ANN001
        if kwargs["post_start_spec"] is None:
            return
        calls["post_start_called"] = True
        calls["post_start_gpu_count"] = kwargs["gpu_count"]

    monkeypatch.setattr(flow_module, "maybe_run_post_start", fake_post_start)
    return ctx, calls


def test_run_notebook_create_orchestrates_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, calls = _configure_create_happy_path(monkeypatch, wait_result=True)

    flow_module.run_notebook_create(
        ctx,
        name=None,
        workspace=None,
        workspace_id=None,
        quota=None,
        project=None,
        image=None,
        shm_size=None,
        auto_stop=True,
        wait=True,
        post_start=None,
        post_start_script=None,
        json_output=False,
        priority=None,
        project_explicit=False,
        group=None,
    )

    # Priority should be capped to the selected project's max priority (6).
    assert calls["task_priority"] == 6
    assert isinstance(calls["quota"], ResolvedQuota)
    assert calls["quota"].quota_id == "quota-h200"
    assert calls["resolve_quota_spec"] == QuotaSpec(
        gpu_count=1, cpu_count=20, memory_gib=200
    )
    assert calls["wait_called"] is True
    assert calls["post_start_called"] is True
    assert calls["post_start_gpu_count"] == 1


def test_run_notebook_create_skips_wait_without_post_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, calls = _configure_create_happy_path(
        monkeypatch, wait_result=True, post_start_value=None
    )

    flow_module.run_notebook_create(
        ctx,
        name=None,
        workspace=None,
        workspace_id=None,
        quota=None,
        project=None,
        image=None,
        shm_size=None,
        auto_stop=True,
        wait=False,
        post_start=None,
        post_start_script=None,
        json_output=False,
        priority=None,
        project_explicit=False,
        group=None,
    )

    assert "wait_called" not in calls
    assert "post_start_called" not in calls


def test_run_notebook_create_skips_post_start_when_wait_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, calls = _configure_create_happy_path(monkeypatch, wait_result=False)

    flow_module.run_notebook_create(
        ctx,
        name=None,
        workspace=None,
        workspace_id=None,
        quota=None,
        project=None,
        image=None,
        shm_size=None,
        auto_stop=True,
        wait=True,
        post_start=None,
        post_start_script=None,
        json_output=False,
        priority=None,
        project_explicit=False,
        group=None,
    )

    assert calls["wait_called"] is True
    assert "post_start_called" not in calls


def test_run_notebook_create_honors_cpu_only_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    cpu_quota = _make_resolved_quota(gpu_count=0, cpu_count=4, memory_gib=32, gpu_type="")
    ctx, calls = _configure_create_happy_path(
        monkeypatch, wait_result=True, resolved_quota=cpu_quota
    )

    flow_module.run_notebook_create(
        ctx,
        name=None,
        workspace=None,
        workspace_id=None,
        quota="0,4,32",
        project=None,
        image=None,
        shm_size=None,
        auto_stop=False,
        wait=True,
        post_start=None,
        post_start_script=None,
        json_output=False,
        priority=None,
        project_explicit=False,
        group=None,
    )

    assert calls["quota"].gpu_count == 0
    # Post-start spec requires GPU by default — CPU-only notebook should skip it.
    assert calls.get("post_start_gpu_count", None) in (None, 0)
