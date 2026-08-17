"""Unit tests for `inspire.platform.web.browser_api.models`."""

from __future__ import annotations

from typing import Any

import pytest

from inspire.platform.web.browser_api import models as models_module
from inspire.platform.web.browser_api.models import (
    ModelInfo,
    check_model_inference_serving_pending,
    check_model_vllm_compatible,
    create_model,
    get_model_detail,
    get_model_publish_prefill,
    get_model_publish_status,
    get_model_recommended_config,
    get_model_vllm_compatibility,
    list_model_inference_servings,
    list_model_users,
    list_model_version_records,
    list_model_versions,
    list_models,
)


class _FakeSession:
    def __init__(self, workspace_id: str | None = "ws-default") -> None:
        self.workspace_id = workspace_id


def _install_fake_request(
    monkeypatch: pytest.MonkeyPatch, response: dict, record: dict
) -> None:
    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):
        record["session"] = session
        record["method"] = method
        record["url"] = url
        record["referer"] = referer
        record["body"] = body
        record["timeout"] = timeout
        return response

    monkeypatch.setattr(models_module, "_request_json", _fake)


def test_list_models_posts_current_filter_shape_and_parses_response(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "code": 0,
            "data": {
                "list": [
                    {
                        "model": {
                            "model_id": "model-1",
                            "id": "42",
                            "name": "demo-model",
                            "status": 2,
                            "version": 3,
                            "project_id": "project-1",
                            "workspace_id": "ws-1",
                            "user_id": "user-1",
                            "is_vllm_compatible": True,
                            "created_at": "1770000000000",
                            "updated_at": "1770000100000",
                            "model_type": ["NaturalLanguageProcessing", "TextGeneration"],
                            "tags": ["demo"],
                            "model_size_gi": 12.5,
                        },
                        "project_name": "Project One",
                        "user_name": "Alice",
                    }
                ],
                "total": 8,
            },
        },
        record,
    )

    items, total = list_models(
        workspace_id="ws-1",
        page=2,
        page_size=10,
        keyword="demo",
        user_id="user-1",
        project_ids=["project-1"],
        session=_FakeSession(),
    )

    assert total == 8
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, ModelInfo)
    assert item.model_id == "model-1"
    assert item.name == "demo-model"
    assert item.latest_version == "3"
    assert item.project_name == "Project One"
    assert item.user_name == "Alice"
    assert item.model_type == ["NaturalLanguageProcessing", "TextGeneration"]
    assert item.tags == ["demo"]
    assert {
        "has_published",
        "is_vllm_compatible",
        "model_path",
        "model_size_gi",
        "version_description",
        "fail_reason",
        "plaza_publish_status",
    }.isdisjoint(item.__dataclass_fields__)
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/model-hub?Action=ListModels")
    assert record["body"] == {
        "page": 2,
        "page_size": 10,
        "filter_by": {
            "keyword": "demo",
            "user_id": "user-1",
            "project_id": ["project-1"],
        },
        "workspace_id": "ws-1",
    }
    assert record["referer"].endswith("/jobs/modelService?spaceId=ws-1")


def test_list_models_rejects_nonzero_code(monkeypatch) -> None:
    _install_fake_request(monkeypatch, {"code": 100002, "message": "bad"}, {})
    with pytest.raises(ValueError, match="API error: bad"):
        list_models(workspace_id="ws-1", user_id="user-1", session=_FakeSession())


def test_model_detail_and_version_endpoints(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"code": 0, "data": {"ok": True}}, record)

    assert get_model_detail("model-1", session=_FakeSession(), workspace_id="ws-1") == {
        "ok": True
    }
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/model-hub?Action=GetModelDetail")
    assert record["body"] == {"model_id": "model-1"}

    # v1 was a REST GET on /model/{id}/versions; v2 is a POST Action. The
    # compact view is ListModelVersionOptions -- ListModelVersions is the
    # richer one that also returns `next_version`.
    assert list_model_versions("model-1", session=_FakeSession()) == {"ok": True}
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/model-hub?Action=ListModelVersionOptions")
    assert record["body"] == {"model_id": "model-1"}

    assert list_model_version_records("model-1", session=_FakeSession()) == {"ok": True}
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/model-hub?Action=ListModelVersions")
    assert record["body"] == {"model_id": "model-1"}


def test_model_version_serving_helpers_use_current_body_shapes(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"code": 0, "data": {"serving": [{"name": "svc"}], "total": "1"}},
        record,
    )

    pending = check_model_inference_serving_pending(
        model_id="model-1",
        version=2,
        session=_FakeSession(),
        workspace_id="ws-1",
    )
    assert pending == {"serving": [{"name": "svc"}], "total": "1"}
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/model-hub?Action=GetHasModelPendingServing")
    assert record["body"] == {"model_id": "model-1", "version": 2}

    items, total = list_model_inference_servings(
        model_id="model-1",
        version="2",
        page=3,
        page_size=5,
        session=_FakeSession(),
        workspace_id="ws-1",
    )
    assert total == 1
    assert items == [{"name": "svc"}]
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/model-hub?Action=ListModelRelatedServings")
    assert record["body"] == {
        "model_id": "model-1",
        "version": 2,
        "page": 3,
        "page_size": 5,
    }


def test_pending_serving_check_drops_the_version_for_the_whole_model(
    monkeypatch,
) -> None:
    """No version means "any version", so the field has to be absent, not 0.

    The platform reads a missing version as the whole-model question and
    `version: 0` as a version that no model has -- sending 0 would answer a
    different question and always answer it "no".
    """
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch, {"Result": {"has_pending_serving": True}}, record
    )

    assert check_model_inference_serving_pending(
        model_id="model-1", session=_FakeSession(), workspace_id="ws-1"
    ) == {"has_pending_serving": True}
    assert record["url"].endswith("/api/v2/model-hub?Action=GetHasModelPendingServing")
    assert record["body"] == {"model_id": "model-1"}


def test_vllm_compatibility_maps_every_version_from_one_request(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "Result": {
                "data": [
                    {"version": 1, "is_vllm_compatible": True},
                    {"version": "2", "is_vllm_compatible": False},
                    {"version": 3},
                    {"is_vllm_compatible": True},
                    "not-a-row",
                ]
            }
        },
        record,
    )

    compatibility = get_model_vllm_compatibility(
        "model-1", session=_FakeSession(), workspace_id="ws-1"
    )

    assert compatibility == {1: True, 2: False, 3: False}
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/model-hub?Action=GetModelVLLMCompatibleData")
    assert record["body"] == {
        "model_id": "model-1",
        "inference_serving_type": "CUSTOM",
    }


def test_vllm_compatibility_returns_empty_when_the_platform_lists_nothing(
    monkeypatch,
) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"Result": {"data": None}}, record)

    assert (
        get_model_vllm_compatibility("model-1", session=_FakeSession()) == {}
    )


def test_vllm_compatibility_raises_instead_of_reporting_incompatible(
    monkeypatch,
) -> None:
    """A refused request must not collapse into "no version is compatible"."""
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"ResponseMetadata": {"Error": {"Code": "AccessForbidden", "Message": "nope"}}},
        record,
    )

    with pytest.raises(ValueError, match="nope"):
        get_model_vllm_compatibility("model-1", session=_FakeSession())


def test_model_publish_helpers_use_version_action(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"Result": {"ok": True}}, record)

    # v1 encoded model + version in the path; v2 takes them in the body.
    assert get_model_publish_prefill(
        "model-1", "4", session=_FakeSession(), workspace_id="ws-1"
    ) == {"ok": True}
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/model-hub?Action=GetModelPublishPrefill")
    assert record["body"] == {"model_id": "model-1", "version": 4}

    assert get_model_publish_status(
        "model-1", 4, session=_FakeSession(), workspace_id="ws-1"
    ) == {"ok": True}
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/model-hub?Action=GetModelPublishStatus")
    assert record["body"] == {"model_id": "model-1", "version": 4}


def test_list_model_users_posts_project_id(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"code": 0, "data": {"list": [{"user_name": "Alice"}], "total": "1"}},
        record,
    )

    items, total = list_model_users(
        "project-1", session=_FakeSession(), workspace_id="ws-1"
    )

    assert total == 1
    assert items == [{"user_name": "Alice"}]
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/model-hub?Action=ListModelCreators")
    assert record["body"] == {"project_id": "project-1"}

def test_create_model_posts_registration_body(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"ResponseMetadata": {}, "Result": {"model_id": "model-new"}},
        record,
    )

    result = create_model(
        name="demo",
        project_id="project-1",
        workspace_id="ws-1",
        model_source_path="/inspire/project/model",
        model_type=["NaturalLanguageProcessing", "TextGeneration"],
        tags=["vllm"],
        description="demo model",
        session=_FakeSession(),
    )

    assert result == {"model_id": "model-new"}
    assert record["method"] == "POST"
    assert record["url"] == "/api/v2/model-hub?Action=CreateModel"
    assert record["body"] == {
        "name": "demo",
        "project_id": "project-1",
        "workspace_id": "ws-1",
        "model_source_path": "/inspire/project/model",
        "model_source_type": 1,
        "model_type": ["NaturalLanguageProcessing", "TextGeneration"],
        "tags": ["vllm"],
        "description": "demo model",
    }


# ---------------------------------------------------------------------------
# Deployment sizing
# ---------------------------------------------------------------------------


def test_get_model_recommended_config_posts_model_and_version(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "Result": {
                "min_node_count": 1,
                "min_gpu_count_per_node": 1,
                "min_cpu_count_per_node": 2,
                "min_memory_size_gib_per_node": 16,
            }
        },
        record,
    )

    result = get_model_recommended_config(
        "model-1", version=2, session=_FakeSession(), workspace_id="ws-1"
    )

    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/model-hub?Action=GetRecommendedConfig")
    assert record["body"] == {"model_id": "model-1", "version": 2}
    assert result["min_gpu_count_per_node"] == 1


def test_get_model_recommended_config_coerces_a_string_version(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"Result": {}}, record)

    get_model_recommended_config("model-1", version="2", session=_FakeSession())  # type: ignore[arg-type]

    # `version` is declared int32; a string is rejected on the wire.
    assert record["body"]["version"] == 2


def test_check_model_vllm_compatible_returns_a_bool(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch, {"Result": {"is_vllm_compatible": True}}, record
    )

    assert (
        check_model_vllm_compatible("model-1", version=1, session=_FakeSession())
        is True
    )
    assert record["url"].endswith("/api/v2/model-hub?Action=CheckModelVLLMCompatible")
    assert record["body"] == {
        "model_id": "model-1",
        "version": 1,
        "inference_serving_type": "CUSTOM",
    }


def test_check_model_vllm_compatible_treats_a_missing_flag_as_false(monkeypatch) -> None:
    # An absent field is not evidence of compatibility.
    _install_fake_request(monkeypatch, {"Result": {}}, {})

    assert (
        check_model_vllm_compatible("model-1", version=1, session=_FakeSession())
        is False
    )
