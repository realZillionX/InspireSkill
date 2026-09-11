"""Image and model registry writes share the single-send contract."""

from __future__ import annotations
import pytest
import requests
from test_sdk import client as client
from inspire import ImageRef, ModelRef, Resource, WorkspaceRef, ProjectRef
from inspire import SubmissionUncertainError, MutationUncertainError, ValidationError
from inspire.platform.web import browser_api as api
from inspire.platform.web.browser_api import images as image_api
from inspire.platform.web.browser_api.images import CustomImageInfo


@pytest.fixture
def catalog(client, monkeypatch):
    ws = Resource(
        "Workspace",
        WorkspaceRef("Workspace", client.account, client.base_url, "ws-test", "ws-test"),
    )
    project = Resource(
        "Project", ProjectRef("Project", client.account, client.base_url, "project-test", "ws-test")
    )
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)
    monkeypatch.setattr(client.projects, "get", lambda *a, **kw: project)


@pytest.mark.parametrize(
    "kind,action",
    [
        ("images", "register"),
        ("images", "delete"),
        ("images", "set_visibility"),
        ("models", "register"),
        ("models", "delete"),
    ],
)
@pytest.mark.parametrize("outcome", ["ok", "timeout", "platform"])
def test_single_dispatch(client, catalog, monkeypatch, kind, action, outcome):
    calls = []

    def once(method, path, body, *a, **kw):
        calls.append((path, body))
        assert client._transport._write is not None
        client._transport._write["sent"] = True
        if outcome == "timeout":
            raise requests.ReadTimeout("lost")
        if outcome == "platform":
            return {
                "ResponseMetadata": {
                    "Error": {"Code": "InvalidParameter", "Message": "平台原始错误"}
                }
            }
        return {
            "Result": {
                "image": {"image_id": "image-test", "address": "registry/image:v2"},
                "model_id": "model-test",
            }
        }

    monkeypatch.setattr(client._transport, "_once", once)

    def invoke():
        service = getattr(client, kind)
        if action == "register":
            if kind == "images":
                return service.register("image", workspace="Workspace", version="v2", visibility="project")
            return service.register(
                "model", source_path="/inspire/models", workspace="Workspace", project="Project", type=["llm"], tag=["tag"]
            )
        cls = ImageRef if kind == "images" else ModelRef
        ref = cls("name", client.account, client.base_url, "key", "ws-test")
        if action == "set_visibility":
            return service.set_visibility(ref, visibility="public")
        return service.delete(ref, **({"force": True} if kind == "models" else {}))

    if outcome == "ok":
        result = invoke()
        if action == "register":
            assert result.ref.key == ("image-test" if kind == "images" else "model-test")
    elif outcome == "platform":
        with pytest.raises(ValidationError) as exc:
            invoke()
        assert "API error: InvalidParameter" in str(exc.value)
        assert "平台原始错误" in str(exc.value)
        assert "平台原始错误" in str(exc.value.__cause__)
    else:
        with pytest.raises(
            SubmissionUncertainError if action == "register" else MutationUncertainError
        ) as exc:
            invoke()
    assert len(calls) == 1
    if kind == "images" and action == "register":
        assert calls[0][1]["add_method"] == 2
        assert calls[0][1]["visibility"] == "VISIBILITY_PROJECT"
        assert calls[0][1]["registry_hint"] == {"workspace_id": "ws-test"}
    if kind == "models" and action == "register":
        assert calls[0][1]["model_type"] == ["llm"] and calls[0][1]["tags"] == ["tag"]
        assert calls[0][1]["model_source_path"] == "/inspire/models"


def test_wait_ready_polls_same_image(client, monkeypatch):
    ref = ImageRef("image:v1", client.account, client.base_url, "image-test", "ws-test")
    states = iter(["BUILDING", "PUSHING", "SUCCESS"])
    calls = []

    def detail(**kw):
        calls.append(kw["image_id"])
        return CustomImageInfo(
            "image-test", "", "image", "", "v1", "SOURCE_PRIVATE", next(states), "", ""
        )

    monkeypatch.setattr(image_api, "get_image_detail", detail)
    monkeypatch.setattr(image_api.time, "sleep", lambda _: None)
    assert client.images.wait_ready(ref, timeout=2).status == "SUCCESS"
    assert calls == ["image-test"] * 3


def test_model_precheck_is_shared_and_force_skips(client, monkeypatch):
    ref = ModelRef("model", client.account, client.base_url, "model-test", "ws-test")
    monkeypatch.setattr(api, "list_model_version_records", lambda *a, **kw: {})
    monkeypatch.setattr(
        api, "check_model_inference_serving_pending", lambda **kw: {"has_pending_serving": True}
    )
    calls = []
    monkeypatch.setattr(api, "delete_model", lambda *a, **kw: calls.append(a))
    with pytest.raises(ValidationError, match="queued"):
        client.models.delete(ref)
    assert not calls
    client.models.delete(ref, force=True)
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["images", "models"])
def test_missing_registration_id_uncertain(client, catalog, monkeypatch, kind):
    monkeypatch.setattr(
        api, "create_image" if kind == "images" else "create_model", lambda **kw: {}
    )
    with pytest.raises(SubmissionUncertainError):
        if kind == "images":
            client.images.register("image", workspace="Workspace")
        else:
            client.models.register("model", source_path="/inspire/models", workspace="Workspace", project="Project")
