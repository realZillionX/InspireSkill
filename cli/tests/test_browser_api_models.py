"""Unit tests for `inspire.platform.web.browser_api.models`."""

from __future__ import annotations

from typing import Any

import pytest

from inspire.platform.web.browser_api import models as models_module
from inspire.platform.web.browser_api.models import (
    ModelInfo,
    check_model_inference_serving_pending,
    create_model,
    get_model_plaza_deploy_serving_config,
    get_model_plaza_detail,
    get_model_plaza_filters,
    get_model_detail,
    get_model_publish_prefill,
    get_model_publish_status,
    list_model_inference_servings,
    list_model_plaza,
    list_model_plaza_related_workspaces,
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


def test_model_plaza_list_filters_and_total_count(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"code": 0, "data": {"items": [{"name": "Qwen"}], "total_count": "12"}},
        record,
    )

    items, total = list_model_plaza(
        workspace_id="ws-1",
        page=2,
        page_size=6,
        keyword="qwen",
        source="MODEL_SOURCE_OPEN",
        model_type="TextGeneration",
        region="domestic",
        min_param_size_b=7,
        max_context_len=32768,
        session=_FakeSession(),
    )

    assert total == 12
    assert items == [{"name": "Qwen"}]
    assert record["method"] == "POST"
    assert record["url"].endswith("/model_plaza/list")
    assert record["body"] == {
        "page": 2,
        "page_size": 6,
        "filter": {
            "workspace_id": "ws-1",
            "keyword": "qwen",
            "source": "MODEL_SOURCE_OPEN",
            "model_type": "TextGeneration",
            "region": "domestic",
            "min_param_size_b": 7,
            "max_context_len": 32768,
        },
    }


def test_model_plaza_get_helpers_use_the_underscored_v2_route(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"ResponseMetadata": {}, "Result": {"items": [{"workspace_id": "ws-1"}]}},
        record,
    )

    assert get_model_plaza_filters(session=_FakeSession()) == {
        "items": [{"workspace_id": "ws-1"}]
    }
    assert record["method"] == "POST"
    # `model-plaza` 404s; only the underscored route resolves.
    assert record["url"] == "/api/v2/model_plaza?Action=GetFilters"
    assert record["body"] == {}

    assert get_model_plaza_detail("mp-1", session=_FakeSession()) == {
        "items": [{"workspace_id": "ws-1"}]
    }
    assert record["url"] == "/api/v2/model_plaza?Action=GetModelDetail"
    assert record["body"] == {"ModelId": "mp-1"}

    items, total = list_model_plaza_related_workspaces("mp-1", session=_FakeSession())
    assert total == 1
    assert items == [{"workspace_id": "ws-1"}]
    assert record["url"] == "/api/v2/model_plaza?Action=GetRelatedWorkspaceList"
    assert record["body"] == {"ModelId": "mp-1"}

    assert get_model_plaza_deploy_serving_config("mp-1", session=_FakeSession()) == {
        "items": [{"workspace_id": "ws-1"}]
    }
    assert record["url"] == "/api/v2/model_plaza?Action=GetDeployServingConfig"
    assert record["body"] == {"ModelId": "mp-1"}


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
