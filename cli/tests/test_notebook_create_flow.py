"""Tests for notebook create flow resource spec resolution."""

from __future__ import annotations

from types import SimpleNamespace

from inspire.cli.utils.notebook_post_start import NotebookPostStartSpec
from inspire.cli.commands.notebook import notebook_create_flow as flow_module
from inspire.cli.commands.notebook.notebook_create_flow import resolve_notebook_resource_spec_price
from inspire.cli.context import Context


def test_cpu_resource_spec_keeps_requested_cpu_from_quota() -> None:
    resource_prices = [
        {
            "gpu_count": 0,
            "cpu_count": 55,
            "memory_size_gib": 220,
            "quota_id": "quota-55",
            "cpu_info": {"cpu_type": "cpu-type-large"},
            "gpu_info": {},
        },
        {
            "gpu_count": 0,
            "cpu_count": 4,
            "memory_size_gib": 16,
            "quota_id": "quota-4",
            "cpu_info": {"cpu_type": "cpu-type-small"},
            "gpu_info": {},
        },
    ]

    spec, resolved_quota, resolved_cpu, resolved_mem = resolve_notebook_resource_spec_price(
        resource_prices=resource_prices,
        gpu_count=0,
        selected_gpu_type="",
        gpu_pattern="CPU",
        logic_compute_group_id="lcg-cpu",
        quota_id="quota-4",
        cpu_count=4,
        memory_size=16,
        requested_cpu_count=4,
    )

    assert resolved_quota == "quota-4"
    assert resolved_cpu == 4
    assert resolved_mem == 16
    assert spec["gpu_count"] == 0
    assert spec["cpu_count"] == 4
    assert spec["memory_size_gib"] == 16
    assert spec["quota_id"] == "quota-4"
    assert spec["cpu_type"] == "cpu-type-small"


def test_cpu_resource_spec_exists_without_resource_prices() -> None:
    spec, resolved_quota, resolved_cpu, resolved_mem = resolve_notebook_resource_spec_price(
        resource_prices=[],
        gpu_count=0,
        selected_gpu_type="",
        gpu_pattern="CPU",
        logic_compute_group_id="lcg-cpu",
        quota_id="quota-4",
        cpu_count=4,
        memory_size=16,
        requested_cpu_count=4,
    )

    assert resolved_quota == "quota-4"
    assert resolved_cpu == 4
    assert resolved_mem == 16
    assert spec["gpu_count"] == 0
    assert spec["cpu_count"] == 4
    assert spec["memory_size_gib"] == 16
    assert spec["quota_id"] == "quota-4"


def test_gpu_resource_spec_prefers_matching_resource_prices() -> None:
    resource_prices = [
        {
            "gpu_count": 1,
            "cpu_count": 20,
            "memory_size_gib": 80,
            "quota_id": "quota-h100",
            "cpu_info": {"cpu_type": "cpu-type-gpu"},
            "gpu_info": {"gpu_type": "NVIDIA_H100"},
        },
        {
            "gpu_count": 8,
            "cpu_count": 64,
            "memory_size_gib": 512,
            "quota_id": "quota-other",
            "cpu_info": {"cpu_type": "cpu-type-other"},
            "gpu_info": {"gpu_type": "NVIDIA_H100"},
        },
    ]

    spec, resolved_quota, resolved_cpu, resolved_mem = resolve_notebook_resource_spec_price(
        resource_prices=resource_prices,
        gpu_count=1,
        selected_gpu_type="NVIDIA_H100",
        gpu_pattern="H100",
        logic_compute_group_id="lcg-h100",
        quota_id="",
        cpu_count=10,
        memory_size=40,
        requested_cpu_count=None,
    )

    assert resolved_quota == "quota-h100"
    assert resolved_cpu == 20
    assert resolved_mem == 80
    assert spec["gpu_count"] == 1
    assert spec["gpu_type"] == "NVIDIA_H100"
    assert spec["cpu_count"] == 20
    assert spec["memory_size_gib"] == 80
    assert spec["quota_id"] == "quota-h100"


def test_gpu_resource_spec_prefers_matching_quota_id_when_present() -> None:
    resource_prices = [
        {
            "gpu_count": 1,
            "cpu_count": 16,
            "memory_size_gib": 64,
            "quota_id": "quota-h200-alt",
            "cpu_info": {"cpu_type": "cpu-type-alt"},
            "gpu_info": {"gpu_type": "NVIDIA_H200_SXM_141G"},
        },
        {
            "gpu_count": 1,
            "cpu_count": 20,
            "memory_size_gib": 80,
            "quota_id": "quota-h200",
            "cpu_info": {"cpu_type": "cpu-type-main"},
            "gpu_info": {"gpu_type": "NVIDIA_H200_SXM_141G"},
        },
    ]

    spec, resolved_quota, resolved_cpu, resolved_mem = resolve_notebook_resource_spec_price(
        resource_prices=resource_prices,
        gpu_count=1,
        selected_gpu_type="NVIDIA_H200_SXM_141G",
        gpu_pattern="H200",
        logic_compute_group_id="lcg-h200",
        quota_id="quota-h200",
        cpu_count=10,
        memory_size=40,
        requested_cpu_count=None,
    )

    assert resolved_quota == "quota-h200"
    assert resolved_cpu == 20
    assert resolved_mem == 80
    assert spec["quota_id"] == "quota-h200"
    assert spec["cpu_type"] == "cpu-type-main"


def test_resolve_notebook_quota_prefers_selected_gpu_type_over_loose_pattern() -> None:
    schedule = {
        "quota": [
            {
                "id": "quota-h100",
                "gpu_count": 1,
                "gpu_type": "NVIDIA_H100",
                "cpu_count": 20,
                "memory_size": 80,
            },
            {
                "id": "quota-4090",
                "gpu_count": 1,
                "gpu_type": "NVIDIA_RTX_4090",
                "cpu_count": 16,
                "memory_size": 64,
            },
        ]
    }

    result = flow_module.resolve_notebook_quota(
        Context(),
        schedule=schedule,
        gpu_count=1,
        gpu_pattern="4090",
        requested_cpu_count=None,
        selected_gpu_type="NVIDIA_RTX_4090",
    )

    assert result == ("quota-4090", 16, 64, "NVIDIA_RTX_4090", "1x4090")


def test_resolve_notebook_quota_matches_equivalent_selected_gpu_labels() -> None:
    schedule = {
        "quota": [
            {
                "id": "quota-h200",
                "gpu_count": 1,
                "gpu_type": "NVIDIA_H200_SXM_141G",
                "cpu_count": 20,
                "memory_size": 80,
            }
        ]
    }

    result = flow_module.resolve_notebook_quota(
        Context(),
        schedule=schedule,
        gpu_count=1,
        gpu_pattern="H200",
        requested_cpu_count=None,
        selected_gpu_type="NVIDIA H200 (141GB)",
    )

    assert result == ("quota-h200", 20, 80, "NVIDIA_H200_SXM_141G", "1xH200")


def test_resolve_notebook_quota_simple_substring_match() -> None:
    schedule = {
        "quota": [
            {
                "id": "quota-h200-1",
                "gpu_count": 1,
                "gpu_type": "NVIDIA_H200_SXM_141G",
                "cpu_count": 20,
                "memory_size": 80,
            },
        ]
    }

    result = flow_module.resolve_notebook_quota(
        Context(),
        schedule=schedule,
        gpu_count=1,
        gpu_pattern="H200",
        requested_cpu_count=None,
        selected_gpu_type="NVIDIA H200 (141GB)",
    )

    assert result == ("quota-h200-1", 20, 80, "NVIDIA_H200_SXM_141G", "1xH200")


def test_resolve_notebook_quota_generic_gpu_wildcard_matches_first_typed_quota() -> None:
    schedule = {
        "quota": [
            {
                "id": "quota-generic",
                "gpu_count": 1,
                "gpu_type": "",
                "cpu_count": 8,
                "memory_size": 32,
            },
            {
                "id": "quota-h100",
                "gpu_count": 1,
                "gpu_type": "NVIDIA_H100",
                "cpu_count": 20,
                "memory_size": 80,
            },
        ]
    }

    result = flow_module.resolve_notebook_quota(
        Context(),
        schedule=schedule,
        gpu_count=1,
        gpu_pattern="GPU",
        requested_cpu_count=None,
        selected_gpu_type="",
    )

    assert result == ("quota-h100", 20, 80, "NVIDIA_H100", "1xGPU")


def test_gpu_resource_spec_matches_generic_gpu_wildcard() -> None:
    resource_prices = [
        {
            "gpu_count": 1,
            "cpu_count": 4,
            "memory_size_gib": 16,
            "quota_id": "quota-blank",
            "cpu_info": {"cpu_type": "cpu-blank"},
            "gpu_info": {"gpu_type": ""},
        },
        {
            "gpu_count": 1,
            "cpu_count": 20,
            "memory_size_gib": 80,
            "quota_id": "quota-h100",
            "cpu_info": {"cpu_type": "cpu-type"},
            "gpu_info": {"gpu_type": "NVIDIA_H100"},
        },
    ]

    spec, resolved_quota, resolved_cpu, resolved_mem = resolve_notebook_resource_spec_price(
        resource_prices=resource_prices,
        gpu_count=1,
        selected_gpu_type="",
        gpu_pattern="GPU",
        logic_compute_group_id="lcg-h100",
        quota_id="",
        cpu_count=10,
        memory_size=40,
        requested_cpu_count=None,
    )

    assert resolved_quota == "quota-h100"
    assert resolved_cpu == 20
    assert resolved_mem == 80
    assert spec["gpu_type"] == "NVIDIA_H100"


def test_resolve_notebook_quota_ignores_empty_gpu_type_when_selected_gpu_type_known() -> None:
    schedule = {
        "quota": [
            {
                "id": "quota-generic",
                "gpu_count": 1,
                "gpu_type": "",
                "cpu_count": 8,
                "memory_size": 32,
            },
            {
                "id": "quota-4090",
                "gpu_count": 1,
                "gpu_type": "NVIDIA_RTX_4090",
                "cpu_count": 16,
                "memory_size": 64,
            },
        ]
    }

    result = flow_module.resolve_notebook_quota(
        Context(),
        schedule=schedule,
        gpu_count=1,
        gpu_pattern="4090",
        requested_cpu_count=None,
        selected_gpu_type="NVIDIA_RTX_4090",
    )

    assert result == ("quota-4090", 16, 64, "NVIDIA_RTX_4090", "1x4090")


def _configure_create_happy_path(
    monkeypatch, *, wait_result: bool, post_start_value: str | None = "echo from config"
) -> tuple[Context, dict[str, object]]:  # noqa: ANN001
    ctx = Context()
    calls: dict[str, object] = {}

    config = SimpleNamespace(
        notebook_resource="1xH100",
        project_order=None,
        job_project_id="project-1111",
        notebook_image=None,
        notebook_post_start=post_start_value,
        job_image="img-default",
        shm_size=32,
        job_priority=9,
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
    monkeypatch.setattr(flow_module, "require_web_session", lambda _ctx, hint: object())
    monkeypatch.setattr(flow_module, "load_config", lambda _ctx: config)
    monkeypatch.setattr(flow_module, "parse_resource_string", lambda _resource: (1, "H100", None))
    monkeypatch.setattr(
        flow_module, "resolve_notebook_workspace_id", lambda *_args, **_kwargs: "ws-1111"
    )
    monkeypatch.setattr(
        flow_module,
        "resolve_notebook_compute_group",
        lambda *_args, **_kwargs: ("lcg-1111", "NVIDIA_H100", "H100", "1xH100"),
    )
    monkeypatch.setattr(flow_module, "_fetch_notebook_schedule", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        flow_module,
        "resolve_notebook_quota",
        lambda *_args, **_kwargs: ("quota-1111", 20, 80, "NVIDIA_H100", "1xH100"),
    )
    monkeypatch.setattr(flow_module, "_fetch_resource_prices", lambda **_kwargs: [])
    monkeypatch.setattr(
        flow_module,
        "resolve_notebook_resource_spec_price",
        lambda **_kwargs: ({"gpu_count": 1}, "quota-1111", 20, 80),
    )
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
        calls["resource_spec_price"] = kwargs["resource_spec_price"]
        calls["quota_id"] = kwargs["quota_id"]
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


def test_run_notebook_create_orchestrates_happy_path(monkeypatch) -> None:  # noqa: ANN001
    ctx, calls = _configure_create_happy_path(monkeypatch, wait_result=True)

    flow_module.run_notebook_create(
        ctx,
        name=None,
        workspace=None,
        workspace_id=None,
        resource=None,
        project=None,
        image=None,
        shm_size=None,
        auto_stop=True,
        auto=False,
        wait=True,
        post_start=None,
        post_start_script=None,
        json_output=False,
        priority=None,
        project_explicit=False,
    )

    # Priority should be capped to the selected project's max priority.
    assert calls["task_priority"] == 6
    assert calls["resource_spec_price"] == {"gpu_count": 1}
    assert calls["wait_called"] is True
    assert calls["post_start_called"] is True
    assert calls["post_start_gpu_count"] == 1


def test_run_notebook_create_skips_wait_without_post_start(monkeypatch) -> None:  # noqa: ANN001
    ctx, calls = _configure_create_happy_path(monkeypatch, wait_result=True, post_start_value=None)

    flow_module.run_notebook_create(
        ctx,
        name=None,
        workspace=None,
        workspace_id=None,
        resource=None,
        project=None,
        image=None,
        shm_size=None,
        auto_stop=True,
        auto=False,
        wait=False,
        post_start=None,
        post_start_script=None,
        json_output=False,
        priority=None,
        project_explicit=False,
    )

    assert "wait_called" not in calls
    assert "post_start_called" not in calls


def test_run_notebook_create_skips_post_start_when_wait_fails(monkeypatch) -> None:  # noqa: ANN001
    ctx, calls = _configure_create_happy_path(monkeypatch, wait_result=False)

    flow_module.run_notebook_create(
        ctx,
        name=None,
        workspace=None,
        workspace_id=None,
        resource=None,
        project=None,
        image=None,
        shm_size=None,
        auto_stop=True,
        auto=False,
        wait=True,
        post_start=None,
        post_start_script=None,
        json_output=False,
        priority=None,
        project_explicit=False,
    )

    assert calls["wait_called"] is True
    assert "post_start_called" not in calls


def test_run_notebook_create_propagates_resolved_quota_to_create(
    monkeypatch,
) -> None:  # noqa: ANN001
    ctx, calls = _configure_create_happy_path(monkeypatch, wait_result=True)

    monkeypatch.setattr(
        flow_module,
        "resolve_notebook_quota",
        lambda *_args, **_kwargs: ("quota-h200", 20, 80, "NVIDIA_H200_SXM_141G", "1xH200"),
    )
    monkeypatch.setattr(
        flow_module,
        "resolve_notebook_resource_spec_price",
        lambda *_args, **_kwargs: (
            {
                "gpu_count": 1,
                "gpu_type": "NVIDIA_H200_SXM_141G",
                "quota_id": "quota-h200",
            },
            "quota-h200",
            20,
            80,
        ),
    )

    flow_module.run_notebook_create(
        ctx,
        name=None,
        workspace=None,
        workspace_id=None,
        resource=None,
        project=None,
        image=None,
        shm_size=None,
        auto_stop=True,
        auto=False,
        wait=True,
        post_start=None,
        post_start_script=None,
        json_output=False,
        priority=None,
        project_explicit=False,
    )

    assert calls["quota_id"] == "quota-h200"
    assert calls["resource_spec_price"]["quota_id"] == "quota-h200"


def test_maybe_run_post_start_warns_when_start_is_not_confirmed(
    monkeypatch, capsys
) -> None:  # noqa: ANN001
    calls: dict[str, object] = {}

    def fake_run_command_in_notebook(**kwargs):  # noqa: ANN003, ANN201
        calls.update(kwargs)
        return False

    monkeypatch.setattr(
        flow_module.browser_api_module,
        "run_command_in_notebook",
        fake_run_command_in_notebook,
    )

    spec = NotebookPostStartSpec(
        label="notebook post-start command",
        command="echo post-start",
        log_path="/tmp/post-start.log",
        pid_file="/tmp/post-start.pid",
        completion_marker="POST_START_READY",
    )

    flow_module.maybe_run_post_start(
        Context(),
        notebook_id="nb-123",
        session=object(),
        post_start_spec=spec,
        gpu_count=1,
        json_output=False,
    )

    captured = capsys.readouterr()
    assert "Starting notebook post-start command..." in captured.out
    assert "Failed to confirm notebook post-start command startup" in captured.err
    assert calls["completion_marker"] == "POST_START_READY"
    assert calls["command"] == "echo post-start"


def test_maybe_wait_for_running_warns_when_no_wait_conflicts_with_post_start(
    monkeypatch, capsys
) -> None:  # noqa: ANN001
    calls: dict[str, object] = {}

    def fake_wait_for_notebook_running(**kwargs):  # noqa: ANN003, ANN201
        calls.update(kwargs)
        return {"status": "RUNNING"}

    monkeypatch.setattr(
        flow_module.browser_api_module,
        "wait_for_notebook_running",
        fake_wait_for_notebook_running,
    )

    ok = flow_module.maybe_wait_for_running(
        Context(),
        notebook_id="nb-123",
        session=object(),
        wait=False,
        needs_post_start=True,
        json_output=False,
        timeout=10,
    )

    captured = capsys.readouterr()
    assert ok is True
    assert "--no-wait requested" in captured.err
    assert "set notebook_post_start=none" in captured.err
    assert "Waiting for notebook to reach RUNNING status..." in captured.out
    assert "Notebook is now RUNNING." in captured.out
    assert calls["notebook_id"] == "nb-123"
    assert calls["timeout"] == 10
