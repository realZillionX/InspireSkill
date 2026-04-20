from types import SimpleNamespace

import pytest

from inspire.platform.openapi.errors import InspireAPIError
from inspire.platform.openapi.hpc_jobs import create_hpc_job


class _DummyAPI:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.endpoints = SimpleNamespace(HPC_JOB_CREATE="/openapi/v1/hpc_jobs/create")

    def _check_authentication(self) -> None:
        return None

    def _validate_required_params(self, **kwargs) -> None:  # noqa: ANN003
        assert kwargs["name"]
        assert kwargs["logic_compute_group_id"]
        assert kwargs["project_id"]
        assert kwargs["workspace_id"]
        assert kwargs["image"]
        assert kwargs["image_type"]
        assert kwargs["entrypoint"]
        assert kwargs["spec_id"]

    def _make_request(self, method: str, endpoint: str, payload: dict) -> dict:
        assert method == "POST"
        assert endpoint == "/openapi/v1/hpc_jobs/create"
        self.calls.append(payload)
        return self._responses.pop(0)


def _invoke(api: _DummyAPI) -> dict:
    return create_hpc_job(
        api,
        name="hpc-demo",
        logic_compute_group_id="lcg-demo",
        project_id="project-demo",
        workspace_id="ws-demo",
        image="registry.local/hpc:latest",
        image_type="SOURCE_PUBLIC",
        entrypoint="bash run.sh",
        spec_id="spec-demo",
        task_priority=3,
        number_of_tasks=1,
        cpus_per_task=4,
        memory_per_cpu=8,
        enable_hyper_threading=False,
    )


def test_hpc_create_falls_back_to_priority_field() -> None:
    api = _DummyAPI(
        responses=[
            {"code": -100000, "message": 'proto: unknown field "task_priority"'},
            {"code": 0, "data": {"job_id": "hpc-job-1"}},
        ]
    )

    result = _invoke(api)
    assert result["code"] == 0
    assert len(api.calls) == 2
    assert "task_priority" in api.calls[0]
    assert "task_priority" not in api.calls[1]
    assert api.calls[1]["priority"] == 3


def test_hpc_create_falls_back_without_any_priority_field() -> None:
    api = _DummyAPI(
        responses=[
            {"code": -100000, "message": 'proto: unknown field "task_priority"'},
            {"code": -100000, "message": 'proto: unknown field "priority"'},
            {"code": 0, "data": {"job_id": "hpc-job-2"}},
        ]
    )

    result = _invoke(api)
    assert result["code"] == 0
    assert len(api.calls) == 3
    assert "task_priority" in api.calls[0]
    assert "priority" in api.calls[1]
    assert "task_priority" not in api.calls[2]
    assert "priority" not in api.calls[2]


def test_hpc_create_raises_for_unrelated_error() -> None:
    api = _DummyAPI(
        responses=[
            {"code": -100000, "message": "invalid spec id"},
        ]
    )

    with pytest.raises(InspireAPIError):
        _invoke(api)
    assert len(api.calls) == 1


def test_hpc_create_invalid_spec_error_includes_hpc_hint() -> None:
    api = _DummyAPI(
        responses=[
            {
                "code": -100000,
                "message": "spec_id spec-demo not found in workspace predef_node_specs",
            },
        ]
    )

    with pytest.raises(InspireAPIError) as exc_info:
        _invoke(api)

    message = str(exc_info.value)
    assert "resources specs --usage hpc" in message
    assert "predef_quota_id" in message
    assert len(api.calls) == 1


def test_hpc_create_retries_with_string_cpu_memory_fields() -> None:
    api = _DummyAPI(
        responses=[
            {"code": -100000, "message": "invalid value for string field memoryPerCpu: 8"},
            {"code": 0, "data": {"job_id": "hpc-job-3"}},
        ]
    )

    result = _invoke(api)
    assert result["code"] == 0
    assert len(api.calls) == 2
    assert api.calls[0]["cpus_per_task"] == "4"
    assert api.calls[0]["memory_per_cpu"] == "8G"
    assert api.calls[1]["cpus_per_task"] == "4"
    assert api.calls[1]["memory_per_cpu"] == "8G"
