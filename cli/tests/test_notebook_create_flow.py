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
        raw_price={
            "cpu_info": {"cpu_type": "Test CPU"},
            "cpu_price_id": "rpc-cpu",
            "cpu_price_version_id": 1,
            "gpu_info": (
                {
                    "gpu_type": "NVIDIA_H200_SXM_141G",
                    "gpu_type_display": "NVIDIA H200 (141GB)",
                }
                if gpu_count
                else {}
            ),
            "gpu_price_id": "rpc-gpu" if gpu_count else "",
            "gpu_price_version_id": 1 if gpu_count else 0,
            "memory_price_id": "rpc-memory",
            "memory_price_version_id": 1,
            "total_price_per_hour": 1 if gpu_count else 0,
        },
    )


def _make_diagnostics() -> flow_module.NotebookCreateDiagnostics:
    return flow_module.NotebookCreateDiagnostics(
        name="fresh-notebook",
        workspace="gpu",
        project="Project One",
        image="Image One",
        resource="1xH200 + 20CPU + 200GiB",
        compute_group="H200 Group",
    )


def test_format_quota_display_gpu() -> None:
    display = flow_module.format_quota_display(_make_resolved_quota())
    assert display == "1xH200 + 20CPU + 200GiB"


def test_format_quota_display_cpu_only() -> None:
    display = flow_module.format_quota_display(
        _make_resolved_quota(gpu_count=0, cpu_count=4, memory_gib=32, gpu_type="")
    )
    assert display == "4CPU + 32GiB"


def test_resolve_create_inputs_requires_explicit_or_profile_quota() -> None:
    config = SimpleNamespace(
        project_order=None,
        shm_size=64,
    )
    with pytest.raises(ValueError, match="--quota is required"):
        flow_module._resolve_create_inputs(
            config=config,
            quota=None,
            project="Project One",
            image="img-x",
            shm_size=None,
        )


def test_resolve_create_inputs_prefers_cli_arg_over_config() -> None:
    config = SimpleNamespace(
        project_order=None,
        shm_size=None,
    )
    quota, _p, _i, shm = flow_module._resolve_create_inputs(
        config=config,
        quota="1,20,200",
        project="Project One",
        image="img-x",
        shm_size=None,
    )
    assert quota == "1,20,200"
    assert shm == 32  # default fallback


def test_resolve_create_inputs_requires_quota_somewhere() -> None:
    config = SimpleNamespace(
        project_order=None,
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
        project_order=None,
        notebook_post_start=post_start_value,
        shm_size=32,
        job_priority=9,
        projects={},
        profiles={},
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
        calls["node_id"] = kwargs.get("node_id")
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
        workspace="gpu",
        workspace_id=None,
        quota="1,20,200",
        project="Project One",
        image="Image One",
        shm_size=None,
        auto_stop=True,
        wait=True,
        post_start=None,
        post_start_script=None,
        json_output=False,
        priority=None,
        project_explicit=False,
        group="H200 Group",
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
        workspace="gpu",
        workspace_id=None,
        quota="1,20,200",
        project="Project One",
        image="Image One",
        shm_size=None,
        auto_stop=True,
        wait=False,
        post_start=None,
        post_start_script=None,
        json_output=False,
        priority=None,
        project_explicit=False,
        group="H200 Group",
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
        workspace="gpu",
        workspace_id=None,
        quota="1,20,200",
        project="Project One",
        image="Image One",
        shm_size=None,
        auto_stop=True,
        wait=True,
        post_start=None,
        post_start_script=None,
        json_output=False,
        priority=None,
        project_explicit=False,
        group="H200 Group",
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
        workspace="gpu",
        workspace_id=None,
        quota="0,4,32",
        project="Project One",
        image="Image One",
        shm_size=None,
        auto_stop=False,
        wait=True,
        post_start=None,
        post_start_script=None,
        json_output=False,
        priority=None,
        project_explicit=False,
        group="CPU Pool",
    )

    assert calls["quota"].gpu_count == 0
    # Post-start spec requires GPU by default — CPU-only notebook should skip it.
    assert calls.get("post_start_gpu_count", None) in (None, 0)


def test_create_notebook_and_report_accepts_id_response_without_human_raw_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = Context()
    quota = _make_resolved_quota()
    selected_project = SimpleNamespace(project_id="project-1111", name="Project One")
    selected_image = SimpleNamespace(
        image_id="img-1111",
        url="docker://image",
        name="Image One",
    )
    captured: dict[str, object] = {}

    def fake_create_notebook(**kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return {"id": "nb-secret-1111"}

    monkeypatch.setattr(flow_module.browser_api_module, "create_notebook", fake_create_notebook)
    monkeypatch.setattr(
        flow_module,
        "_resolve_created_notebook_id",
        lambda **_kwargs: pytest.fail("lookup should not run when id is present"),
    )

    notebook_id = flow_module.create_notebook_and_report(
        ctx,
        name="fresh-notebook",
        resource_display="1xH200 + 20CPU + 200GiB",
        diagnostics=_make_diagnostics(),
        selected_project=selected_project,
        selected_image=selected_image,
        quota=quota,
        shm_size=64,
        auto_stop=True,
        workspace_id="ws-1111",
        session=SimpleNamespace(all_workspace_names={"ws-1111": "gpu"}),
        json_output=False,
        task_priority=5,
    )

    out = capsys.readouterr().out
    assert notebook_id == "nb-secret-1111"
    assert captured["project_id"] == "project-1111"
    assert captured["image_id"] == "img-1111"
    assert captured["logic_compute_group_id"] == "lcg-test"
    assert captured["quota_id"] == "quota-h200"
    assert captured["shared_memory_size"] == 64
    assert captured["task_priority"] == 5
    assert captured["resource_spec_price"] == {
        "cpu_type": "Test CPU",
        "cpu_count": 20,
        "gpu_type": "NVIDIA_H200_SXM_141G",
        "gpu_count": 1,
        "memory_size_gib": 200,
        "logic_compute_group_id": "lcg-test",
        "quota_id": "quota-h200",
    }
    assert "Notebook created successfully!" in out
    assert "Name: fresh-notebook" in out
    assert "Workspace: gpu" in out
    assert "Compute group: H200 Group" in out
    assert "inspire notebook events fresh-notebook" in out
    assert "inspire notebook ssh connect fresh-notebook --workspace gpu" in out
    assert 'inspire notebook exec fresh-notebook "pwd"' in out
    assert "inspire notebook delete fresh-notebook --workspace gpu --yes" in out
    assert "nb-secret-1111" not in out
    assert "ID:" not in out


def test_create_notebook_and_report_missing_id_reports_create_context(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = Context()
    quota = _make_resolved_quota()
    selected_project = SimpleNamespace(project_id="project-1111", name="Project One")
    selected_image = SimpleNamespace(
        image_id="img-1111",
        url="docker://image",
        name="Image One",
    )

    monkeypatch.setattr(flow_module.browser_api_module, "create_notebook", lambda **_kwargs: {})
    monkeypatch.setattr(flow_module, "_resolve_created_notebook_id", lambda **_kwargs: "")

    with pytest.raises(SystemExit):
        flow_module.create_notebook_and_report(
            ctx,
            name="fresh-notebook",
            resource_display="1xH200 + 20CPU + 200GiB",
            diagnostics=_make_diagnostics(),
            selected_project=selected_project,
            selected_image=selected_image,
            quota=quota,
            shm_size=64,
            auto_stop=True,
            workspace_id="ws-1111",
            session=SimpleNamespace(all_workspace_names={"ws-1111": "gpu"}),
            json_output=False,
            task_priority=5,
        )

    err = capsys.readouterr().err
    assert "Notebook 'fresh-notebook' was submitted" in err
    assert "Notebook: fresh-notebook" in err
    assert "Workspace: gpu" in err
    assert "Project: Project One" in err
    assert "Compute group: H200 Group" in err
    assert "Platform events: no platform events returned yet." in err
    assert "nb-" not in err


def test_wait_failure_reports_events_and_suppresses_raw_notebook_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = Context()

    def fake_wait(**_kwargs):  # noqa: ANN001
        raise flow_module.NotebookFailedError(
            "nb-secret-2222",
            "FAILED",
            {
                "status": "FAILED",
                "sub_status": "GPU_ALLOC_ERROR",
                "extra_info": {"NodeName": "node-a"},
            },
            events="FailedScheduling: notebook nb-secret-2222 cannot allocate GPU",
        )

    monkeypatch.setattr(flow_module.browser_api_module, "wait_for_notebook_running", fake_wait)

    with pytest.raises(SystemExit):
        flow_module.maybe_wait_for_running(
            ctx,
            notebook_id="nb-secret-2222",
            diagnostics=_make_diagnostics(),
            session=object(),
            wait=True,
            needs_post_start=False,
            json_output=False,
            timeout=1,
        )

    err = capsys.readouterr().err
    assert "Notebook 'fresh-notebook' failed to start." in err
    assert "Notebook: fresh-notebook" in err
    assert "Workspace: gpu" in err
    assert "Project: Project One" in err
    assert "Compute group: H200 Group" in err
    assert "terminal status: FAILED" in err
    assert "sub-status: GPU_ALLOC_ERROR" in err
    assert "Platform events:" in err
    assert "FailedScheduling: notebook <notebook-id> cannot allocate GPU" in err
    assert "NodeName: node-a" in err
    assert "nb-secret-2222" not in err


def test_run_notebook_create_threads_node(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, calls = _configure_create_happy_path(
        monkeypatch, wait_result=True, post_start_value=None
    )

    flow_module.run_notebook_create(
        ctx,
        name="pinned-nb",
        workspace="gpu",
        workspace_id=None,
        quota="1,20,200",
        project="Project One",
        image="Image One",
        shm_size=None,
        auto_stop=False,
        wait=False,
        post_start=None,
        post_start_script=None,
        json_output=False,
        priority=None,
        project_explicit=False,
        group="H200 Group",
        node="qb-prod-gpu1736",
    )

    assert calls["node_id"] == "qb-prod-gpu1736"


def test_run_notebook_create_node_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, calls = _configure_create_happy_path(
        monkeypatch, wait_result=True, post_start_value=None
    )

    flow_module.run_notebook_create(
        ctx,
        name="unpinned-nb",
        workspace="gpu",
        workspace_id=None,
        quota="1,20,200",
        project="Project One",
        image="Image One",
        shm_size=None,
        auto_stop=False,
        wait=False,
        post_start=None,
        post_start_script=None,
        json_output=False,
        priority=None,
        project_explicit=False,
        group="H200 Group",
    )

    assert calls["node_id"] is None


def test_create_notebook_payload_includes_node_id_when_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inspire.platform.web.browser_api import notebooks as notebooks_module

    captured: dict[str, object] = {}

    def fake_request(_session, _method, _path, *, body=None, **_kwargs):  # noqa: ANN001
        captured["body"] = body
        return {"id": "nb-1"}

    monkeypatch.setattr(notebooks_module, "_request_notebooks_data", fake_request)

    notebooks_module.create_notebook(
        name="n",
        project_id="project-1",
        project_name="P",
        image_id="img-1",
        image_url="docker://image",
        logic_compute_group_id="lcg-1",
        quota_id="quota-1",
        gpu_type="H200",
        gpu_count=1,
        cpu_count=20,
        memory_size=200,
        shared_memory_size=64,
        auto_stop=False,
        workspace_id="ws-1",
        session=SimpleNamespace(workspace_id="ws-1"),
        node_id="qb-prod-gpu1736",
    )

    assert captured["body"]["node_id"] == "qb-prod-gpu1736"


def test_create_notebook_payload_omits_node_id_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inspire.platform.web.browser_api import notebooks as notebooks_module

    captured: dict[str, object] = {}

    def fake_request(_session, _method, _path, *, body=None, **_kwargs):  # noqa: ANN001
        captured["body"] = body
        return {"id": "nb-1"}

    monkeypatch.setattr(notebooks_module, "_request_notebooks_data", fake_request)

    notebooks_module.create_notebook(
        name="n",
        project_id="project-1",
        project_name="P",
        image_id="img-1",
        image_url="docker://image",
        logic_compute_group_id="lcg-1",
        quota_id="quota-1",
        gpu_type="H200",
        gpu_count=1,
        cpu_count=20,
        memory_size=200,
        shared_memory_size=64,
        auto_stop=False,
        workspace_id="ws-1",
        session=SimpleNamespace(workspace_id="ws-1"),
    )

    assert "node_id" not in captured["body"]
