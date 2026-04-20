from types import SimpleNamespace

from inspire.platform.openapi.jobs import create_training_job_smart


class _DummyResourceManager:
    def get_recommended_config(self, resource: str, prefer_location: str | None) -> tuple[str, str]:
        assert resource == "1xH200"
        assert prefer_location is None
        return "spec-1x-h200", "lcg-h200-1"


class _DummyAPI:
    DEFAULT_PROJECT_ID = "project-default"
    DEFAULT_WORKSPACE_ID = "ws-default"
    DEFAULT_TASK_PRIORITY = 10
    DEFAULT_INSTANCE_COUNT = 1
    DEFAULT_MAX_RUNNING_TIME = "3600000"
    DEFAULT_SHM_SIZE = 128
    DEFAULT_IMAGE_TYPE = "SOURCE_PRIVATE"

    def __init__(self) -> None:
        self.resource_manager = _DummyResourceManager()
        self.endpoints = SimpleNamespace(TRAIN_JOB_CREATE="/openapi/v1/train_job/create")
        self.config = SimpleNamespace(docker_registry=None)
        self.last_request: tuple[str, str, dict] | None = None

    def _check_authentication(self) -> None:  # noqa: D401
        return None

    def _validate_required_params(self, **kwargs) -> None:  # noqa: ANN003
        assert kwargs["name"]
        assert kwargs["command"]
        assert kwargs["resource"]

    def _get_default_image(self) -> str:
        return "registry.local/default:latest"

    def _make_request(self, method: str, endpoint: str, payload: dict) -> dict:
        self.last_request = (method, endpoint, payload)
        return {"code": 0, "data": {"job_id": "job-123"}}


def test_create_training_job_smart_builds_framework_config_payload() -> None:
    api = _DummyAPI()

    create_training_job_smart(
        api,
        name="demo",
        command="echo demo",
        resource="1xH200",
    )

    assert api.last_request is not None
    method, endpoint, payload = api.last_request
    assert method == "POST"
    assert endpoint == "/openapi/v1/train_job/create"

    assert payload["command"] == "echo demo"
    assert payload["logic_compute_group_id"] == "lcg-h200-1"
    assert payload["project_id"] == "project-default"
    assert payload["workspace_id"] == "ws-default"
    assert payload["framework_config"] == [
        {
            "image_type": "SOURCE_PRIVATE",
            "image": "registry.local/default:latest",
            "instance_count": 1,
            "spec_id": "spec-1x-h200",
            "shm_gi": 128,
        }
    ]
    assert "start_cmd" not in payload
    assert "spec_id" not in payload
    assert "image" not in payload
    assert "instance_count" not in payload
    assert "shm_gi" not in payload


def test_create_training_job_smart_uses_overrides_for_framework_config() -> None:
    api = _DummyAPI()

    create_training_job_smart(
        api,
        name="demo",
        command="echo demo",
        resource="1xH200",
        image="custom.registry/pytorch:tag",
        instance_count=2,
        shm_gi=256,
    )

    assert api.last_request is not None
    payload = api.last_request[2]
    framework_item = payload["framework_config"][0]
    assert framework_item["image"] == "custom.registry/pytorch:tag"
    assert framework_item["instance_count"] == 2
    assert framework_item["shm_gi"] == 256
