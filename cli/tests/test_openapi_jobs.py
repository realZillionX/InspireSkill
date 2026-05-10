"""OpenAPI create_training_job_smart payload tests.

The function requires pre-resolved ``spec_id_override`` +
``compute_group_id_override`` (the CLI resolves them via
``inspire.cli.utils.quota_resolver.resolve_quota``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from inspire.platform.openapi.errors import ValidationError
from inspire.platform.openapi.jobs import create_training_job_smart
from inspire.job_defaults import DEFAULT_TRAINING_MAX_TIME_MS


class _DummyAPI:
    DEFAULT_TASK_PRIORITY = 10
    DEFAULT_MAX_RUNNING_TIME = DEFAULT_TRAINING_MAX_TIME_MS
    DEFAULT_SHM_SIZE = 128
    DEFAULT_IMAGE_TYPE = "SOURCE_PRIVATE"

    def __init__(self) -> None:
        self.endpoints = SimpleNamespace(TRAIN_JOB_CREATE="/openapi/v1/train_job/create")
        self.config = SimpleNamespace(docker_registry=None)
        self.last_request: tuple[str, str, dict] | None = None

    def _check_authentication(self) -> None:
        return None

    def _validate_required_params(self, **kwargs) -> None:  # noqa: ANN003
        missing = [key for key, value in kwargs.items() if value in (None, "")]
        if missing:
            raise ValidationError(f"Missing required parameters: {', '.join(missing)}")

    def _make_request(self, method: str, endpoint: str, payload: dict) -> dict:
        self.last_request = (method, endpoint, payload)
        return {"code": 0, "data": {"job_id": "job-123"}}


def _required_job_fields() -> dict[str, object]:
    return {
        "project_id": "project-explicit",
        "workspace_id": "ws-explicit",
        "image": "registry.local/explicit:latest",
        "instance_count": 1,
    }


def test_create_training_job_smart_builds_framework_config_payload() -> None:
    api = _DummyAPI()

    create_training_job_smart(
        api,
        name="demo",
        command="echo demo",
        **_required_job_fields(),
        spec_id_override="spec-1x-h200",
        compute_group_id_override="lcg-h200-1",
    )

    assert api.last_request is not None
    method, endpoint, payload = api.last_request
    assert method == "POST"
    assert endpoint == "/openapi/v1/train_job/create"

    assert payload["command"] == "echo demo"
    assert payload["logic_compute_group_id"] == "lcg-h200-1"
    assert payload["project_id"] == "project-explicit"
    assert payload["workspace_id"] == "ws-explicit"
    assert payload["max_running_time_ms"] == DEFAULT_TRAINING_MAX_TIME_MS
    assert payload["framework_config"] == [
        {
            "image_type": "SOURCE_PRIVATE",
            "image": "registry.local/explicit:latest",
            "instance_count": 1,
            "spec_id": "spec-1x-h200",
            "shm_gi": 128,
        }
    ]


def test_create_training_job_smart_uses_overrides_for_framework_config() -> None:
    api = _DummyAPI()

    create_training_job_smart(
        api,
        name="demo",
        command="echo demo",
        project_id="project-explicit",
        workspace_id="ws-explicit",
        image="custom.registry/pytorch:tag",
        instance_count=2,
        shm_gi=256,
        spec_id_override="spec-1x-h200",
        compute_group_id_override="lcg-h200-1",
    )

    payload = api.last_request[2]
    framework_item = payload["framework_config"][0]
    assert framework_item["image"] == "custom.registry/pytorch:tag"
    assert framework_item["instance_count"] == 2
    assert framework_item["shm_gi"] == 256


def test_create_training_job_smart_requires_overrides() -> None:
    """Without pre-resolved spec+compute_group, the call must fail loudly."""
    api = _DummyAPI()
    with pytest.raises(ValidationError, match="spec_id_override"):
        create_training_job_smart(
            api,
            name="demo",
            command="echo demo",
            **_required_job_fields(),
        )


def test_create_training_job_smart_requires_create_fields() -> None:
    api = _DummyAPI()
    with pytest.raises(ValidationError, match="project_id"):
        create_training_job_smart(
            api,
            name="demo",
            command="echo demo",
            spec_id_override="spec-1x-h200",
            compute_group_id_override="lcg-h200-1",
        )


def test_create_training_job_smart_fault_tolerance_off_by_default() -> None:
    """When auto_fault_tolerance is not set, neither field appears in the payload."""
    api = _DummyAPI()
    create_training_job_smart(
        api,
        name="demo",
        command="echo demo",
        **_required_job_fields(),
        spec_id_override="spec-1x-h200",
        compute_group_id_override="lcg-h200-1",
    )
    payload = api.last_request[2]
    assert "auto_fault_tolerance" not in payload
    assert "fault_tolerance_max_retry" not in payload


def test_create_training_job_smart_fault_tolerance_enabled_default_retry() -> None:
    """auto_fault_tolerance=True without explicit retry defaults fault_tolerance_max_retry to 10."""
    api = _DummyAPI()
    create_training_job_smart(
        api,
        name="demo",
        command="echo demo",
        **_required_job_fields(),
        spec_id_override="spec-1x-h200",
        compute_group_id_override="lcg-h200-1",
        auto_fault_tolerance=True,
    )
    payload = api.last_request[2]
    assert payload["auto_fault_tolerance"] is True
    assert payload["fault_tolerance_max_retry"] == 10


def test_create_training_job_smart_fault_tolerance_enabled_explicit_retry() -> None:
    """auto_fault_tolerance=True with explicit retry uses the provided value."""
    api = _DummyAPI()
    create_training_job_smart(
        api,
        name="demo",
        command="echo demo",
        **_required_job_fields(),
        spec_id_override="spec-1x-h200",
        compute_group_id_override="lcg-h200-1",
        auto_fault_tolerance=True,
        fault_tolerance_max_retry=5,
    )
    payload = api.last_request[2]
    assert payload["auto_fault_tolerance"] is True
    assert payload["fault_tolerance_max_retry"] == 5


def test_create_training_job_smart_fault_tolerance_false_excludes_fields() -> None:
    """auto_fault_tolerance=False must not add either field to the payload."""
    api = _DummyAPI()
    create_training_job_smart(
        api,
        name="demo",
        command="echo demo",
        **_required_job_fields(),
        spec_id_override="spec-1x-h200",
        compute_group_id_override="lcg-h200-1",
        auto_fault_tolerance=False,
        fault_tolerance_max_retry=5,
    )
    payload = api.last_request[2]
    assert "auto_fault_tolerance" not in payload
    assert "fault_tolerance_max_retry" not in payload


def test_create_training_job_smart_fault_tolerance_invalid_retry() -> None:
    """fault_tolerance_max_retry < 1 with auto_fault_tolerance=True must raise ValidationError."""
    api = _DummyAPI()
    with pytest.raises(ValidationError, match="fault_tolerance_max_retry must be >= 1"):
        create_training_job_smart(
            api,
            name="demo",
            command="echo demo",
            **_required_job_fields(),
            spec_id_override="spec-1x-h200",
            compute_group_id_override="lcg-h200-1",
            auto_fault_tolerance=True,
            fault_tolerance_max_retry=0,
        )
