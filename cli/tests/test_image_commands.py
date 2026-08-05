"""Tests for image management commands and API functions."""

import json
from pathlib import Path
from typing import Any, Optional

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.image import image_commands as image_commands_module
from inspire.cli.commands.notebook import notebook_lookup as notebook_lookup_module
from inspire.cli.context import Context
from inspire.cli.main import main as cli_main
from inspire.cli.utils.resource_index import (
    ResourceIdentity,
    ResourceIndex,
    ResourceScope,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.platform.web.browser_api.images import (
    CustomImageInfo,
    _image_from_api,
    list_images_by_source,
)


_FORBIDDEN_PUBLIC_KEYS = {
    "id",
    "image_id",
    "raw",
    "payload",
    "result",
    "scanned",
    "source",
}

_REAL_RESOLVE_IMAGE_NAME = image_commands_module._resolve_image_name


def _json_data(output: str) -> Any:
    parsed = json.loads(output)
    return parsed.get("data", parsed)


def _assert_compact_public_payload(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in _FORBIDDEN_PUBLIC_KEYS
            assert not key.endswith("_id")
            assert not key.endswith("_ids")
            _assert_compact_public_payload(child)
    elif isinstance(value, list):
        for child in value:
            _assert_compact_public_payload(child)


def _assert_safe_failure(output: str, expected: str) -> None:
    assert expected in output
    assert "/Users/alice/private.log" not in output
    assert "img-secret-123" not in output
    assert "request payload" not in output


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class FakeWebSession:
    workspace_id = "ws-test-workspace"
    all_workspace_ids = ["ws-test-workspace"]
    all_workspace_names = {"ws-test-workspace": "Test Workspace"}
    storage_state = {}


def _make_config(tmp_path: Path) -> config_module.Config:
    return config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        path_aliases={"me": str(tmp_path / "logs")},
    )


def _patch_config_and_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> config_module.Config:
    config = _make_config(tmp_path)

    def fake_from_files_and_env(
        cls,
        require_credentials: bool = True,
    ) -> tuple:
        return config, {}

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(fake_from_files_and_env),
    )
    monkeypatch.setattr(
        web_session_module,
        "get_web_session",
        lambda: FakeWebSession(),
    )
    monkeypatch.setattr(
        notebook_lookup_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: ("notebook-abc", None),
    )
    return config


def _patch_image_candidates(
    monkeypatch: pytest.MonkeyPatch, *images: CustomImageInfo
) -> None:
    monkeypatch.setattr(
        browser_api_module,
        "list_images_by_source",
        lambda source="official", session=None: list(images),
    )


def _patch_image_name_resolver(
    monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]
) -> None:
    def _fake_resolve(
        ctx,
        name: str,
        *,
        pick: Optional[int] = None,
        **_kwargs,
    ) -> str:  # noqa: ANN001
        del ctx, pick
        return mapping[name]

    monkeypatch.setattr(image_commands_module, "_resolve_image_name", _fake_resolve)


def _cacheable_image_session() -> FakeWebSession:
    session = FakeWebSession()
    session.base_url = "https://inspire.example"
    session.user_detail = {"id": "user-one"}
    session.login_username = "alice"
    return session


def _image_scope() -> ResourceScope:
    return ResourceScope(
        base_url="https://inspire.example",
        subject_id="user-one",
        resource_type="image",
        workspace_id="ws-test-workspace",
        owner_scope="self",
    )


# ---------------------------------------------------------------------------
# Model / helper tests
# ---------------------------------------------------------------------------


def test_image_from_api_parses_fields():
    raw = {
        "image_id": "img-123",
        "address": "registry.example/my-image:v1",
        "name": "my-image",
        "framework": "pytorch",
        "version": "2.1",
        "source": "SOURCE_PRIVATE",
        "status": "READY",
        "description": "Test image",
        "created_at": "2026-01-15T10:00:00Z",
    }
    img = _image_from_api(raw)
    assert img.image_id == "img-123"
    assert img.url == "registry.example/my-image:v1"
    assert img.name == "my-image"
    assert img.framework == "pytorch"
    assert img.version == "2.1"
    assert img.source == "SOURCE_PRIVATE"
    assert img.status == "READY"
    assert img.description == "Test image"
    assert img.created_at == "2026-01-15T10:00:00Z"


def test_image_from_api_derives_name_from_address():
    raw = {"image_id": "img-456", "address": "registry.example/org/cool-image:latest"}
    img = _image_from_api(raw)
    assert img.name == "cool-image:latest"


def test_image_from_api_handles_missing_fields():
    img = _image_from_api({})
    assert img.image_id == ""
    assert img.name == ""
    assert img.source == ""


def test_list_images_by_source_official(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def fake_request_notebooks_data(
        session,
        method: str,
        endpoint_path: str,
        *,
        body: Optional[dict] = None,
        timeout: int = 30,
        default_data: Any = None,
    ) -> Any:
        captured["method"] = method
        captured["body"] = body
        return {
            "images": [
                {
                    "image_id": "img-off-001",
                    "address": "registry/official-img",
                    "name": "official-img",
                    "framework": "TensorFlow",
                    "version": "2.12",
                    "source": "SOURCE_OFFICIAL",
                    "status": "READY",
                    "description": "",
                    "created_at": "2025-12-01",
                }
            ]
        }

    from inspire.platform.web.browser_api import images as images_module

    monkeypatch.setattr(images_module, "_request_notebooks_data", fake_request_notebooks_data)
    monkeypatch.setattr(
        images_module,
        "_get_session_and_workspace_id",
        lambda workspace_id, session: (FakeWebSession(), "ws-test"),
    )

    results = list_images_by_source(source="official")
    assert len(results) == 1
    assert results[0].image_id == "img-off-001"
    assert results[0].source == "SOURCE_OFFICIAL"
    assert results[0].status == "READY"
    assert captured["body"]["filter"]["source"] == "SOURCE_OFFICIAL"


def test_list_images_by_source_public(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def fake_request_notebooks_data(
        session,
        method: str,
        endpoint_path: str,
        *,
        body: Optional[dict] = None,
        timeout: int = 30,
        default_data: Any = None,
    ) -> Any:
        captured["body"] = body
        return {"images": []}

    from inspire.platform.web.browser_api import images as images_module

    monkeypatch.setattr(images_module, "_request_notebooks_data", fake_request_notebooks_data)
    monkeypatch.setattr(
        images_module,
        "_get_session_and_workspace_id",
        lambda workspace_id, session: (FakeWebSession(), "ws-test"),
    )

    results = list_images_by_source(source="public")
    assert results == []
    # Public uses source_list + visibility filter
    assert captured["body"]["filter"]["visibility"] == "VISIBILITY_PUBLIC"
    assert "SOURCE_PUBLIC" in captured["body"]["filter"]["source_list"]


def test_list_images_by_source_private_personal_visible(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def fake_request_notebooks_data(
        session,
        method: str,
        endpoint_path: str,
        *,
        body: Optional[dict] = None,
        timeout: int = 30,
        default_data: Any = None,
    ) -> Any:
        captured["body"] = body
        return {"images": []}

    from inspire.platform.web.browser_api import images as images_module

    monkeypatch.setattr(images_module, "_request_notebooks_data", fake_request_notebooks_data)
    monkeypatch.setattr(
        images_module,
        "_get_session_and_workspace_id",
        lambda workspace_id, session: (FakeWebSession(), "ws-test"),
    )

    results = list_images_by_source(source="private")
    assert results == []
    assert captured["body"]["filter"]["visibility"] == "VISIBILITY_PRIVATE"
    assert "SOURCE_PRIVATE" in captured["body"]["filter"]["source_list"]
    assert "SOURCE_PUBLIC" in captured["body"]["filter"]["source_list"]
    assert "source" not in captured["body"]["filter"]


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


def test_image_help_includes_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["image", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "detail" in result.output
    assert "register" in result.output
    assert "save" in result.output
    assert "delete" in result.output


@pytest.mark.parametrize(
    "command",
    [
        ["list"],
        ["detail"],
        ["register"],
        ["save"],
        ["set-visibility"],
        ["delete"],
    ],
)
def test_image_command_help_is_compact_and_name_only(command: list[str]) -> None:
    result = CliRunner().invoke(cli_main, ["image", *command, "--help"])

    assert result.exit_code == 0, result.output
    assert "Examples:" not in result.output
    assert "image_id" not in result.output
    assert "IMAGE_ID" not in result.output


def test_image_register_help_keeps_push_workflow_requirements() -> None:
    result = CliRunner().invoke(cli_main, ["image", "register", "--help"])

    assert result.exit_code == 0, result.output
    assert "registry-specific" in result.output
    assert "docker tag" in result.output
    assert "docker push" in result.output


def test_image_list_human_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    calls: list[str] = []

    def fake_list_by_source(source="official", session=None):
        calls.append(source)
        if source == "official":
            return [
                browser_api_module.CustomImageInfo(
                    image_id="img-001",
                    url="registry/pytorch:2.0",
                    name="pytorch",
                    framework="PyTorch",
                    version="2.0",
                    source="SOURCE_OFFICIAL",
                    status="READY",
                    description="",
                    created_at="",
                )
            ]
        if source == "public":
            return [
                browser_api_module.CustomImageInfo(
                    image_id="img-pub-001",
                    url="registry/lyz-dev:100",
                    name="lyz-dev:100",
                    framework="PyTorch",
                    version="100",
                    source="SOURCE_PUBLIC",
                    status="SUCCESS",
                    description="",
                    created_at="",
                )
            ]
        return []

    monkeypatch.setattr(
        browser_api_module,
        "list_images_by_source",
        fake_list_by_source,
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["image", "list"])
    assert result.exit_code == 0
    assert calls == ["official", "public", "private"]
    assert "pytorch" in result.output
    assert "lyz-dev:100" in result.output
    assert "2.0" in result.output
    assert "official" in result.output
    assert "READY" in result.output
    assert "Total:" not in result.output
    assert "img-001" not in result.output
    assert "img-pub-001" not in result.output


def test_image_list_json_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    monkeypatch.setattr(
        browser_api_module,
        "list_images_by_source",
        lambda source="official", session=None: [
            browser_api_module.CustomImageInfo(
                image_id="img-001",
                url="registry/pytorch:2.0",
                name="pytorch",
                framework="PyTorch",
                version="2.0",
                source="SOURCE_OFFICIAL",
                status="READY",
                description="",
                created_at="",
            )
        ],
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "image", "list"])
    assert result.exit_code == 0

    payload = _json_data(result.output)
    assert len(payload["items"]) == 1
    assert payload["items"][0] == {
        "name": "pytorch:2.0",
        "status": "READY",
        "framework": "PyTorch",
        "visibility": "official",
    }
    assert "images" not in payload
    assert "total" not in payload
    _assert_compact_public_payload(payload)
    assert "img-001" not in result.output


def test_image_list_private_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    monkeypatch.setattr(
        browser_api_module,
        "list_images_by_source",
        lambda source="official", session=None: [
            browser_api_module.CustomImageInfo(
                image_id="img-priv-001",
                url="registry/my-custom:v1",
                name="personal-visible-img",
                framework="pytorch",
                version="2.1",
                source="SOURCE_PUBLIC",
                status="READY",
                description="Custom image",
                created_at="2026-01-10",
            )
        ],
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "image", "list", "--source", "private"])
    assert result.exit_code == 0

    payload = _json_data(result.output)
    assert len(payload["items"]) == 1
    assert payload["items"][0]["name"] == "personal-visible-img:2.1"
    assert payload["items"][0]["visibility"] == "public"
    _assert_compact_public_payload(payload)


def test_image_list_all_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    def fake_list_by_source(source="official", session=None):
        if source == "official":
            return [
                browser_api_module.CustomImageInfo(
                    image_id="img-off",
                    url="registry/off",
                    name="official-img",
                    framework="TF",
                    version="1.0",
                    source="SOURCE_OFFICIAL",
                    status="READY",
                    description="",
                    created_at="",
                )
            ]
        elif source == "private":
            return [
                browser_api_module.CustomImageInfo(
                    image_id="img-shared",
                    url="registry/personal-visible",
                    name="personal-visible-img",
                    framework="PT",
                    version="2.0",
                    source="SOURCE_PUBLIC",
                    status="READY",
                    description="",
                    created_at="",
                )
            ]
        elif source == "public":
            return [
                browser_api_module.CustomImageInfo(
                    image_id="img-pub",
                    url="registry/pub",
                    name="public-img",
                    framework="PT",
                    version="1.9",
                    source="SOURCE_PUBLIC",
                    status="READY",
                    description="",
                    created_at="",
                )
            ]
        return []

    monkeypatch.setattr(
        browser_api_module,
        "list_images_by_source",
        fake_list_by_source,
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "image", "list", "--source", "all"])
    assert result.exit_code == 0

    payload = _json_data(result.output)
    # official + public + private
    assert len(payload["items"]) == 3
    names = [img["name"] for img in payload["items"]]
    assert "official-img:1.0" in names
    assert "public-img:1.9" in names
    assert "personal-visible-img:2.0" in names
    assert "total" not in payload
    _assert_compact_public_payload(payload)


def test_image_list_all_sources_partial_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    def fake_list_by_source(source="official", session=None):
        if source == "public":
            raise RuntimeError(
                "socket hang up at /Users/alice/private.log image_id=img-secret-123"
            )
        if source == "official":
            return [
                browser_api_module.CustomImageInfo(
                    image_id="img-off",
                    url="registry/off",
                    name="official-img",
                    framework="TF",
                    version="1.0",
                    source="SOURCE_OFFICIAL",
                    status="READY",
                    description="",
                    created_at="",
                )
            ]
        if source == "private":
            return [
                browser_api_module.CustomImageInfo(
                    image_id="img-priv",
                    url="registry/priv",
                    name="personal-visible-img",
                    framework="PT",
                    version="2.0",
                    source="SOURCE_PUBLIC",
                    status="READY",
                    description="",
                    created_at="",
                )
            ]
        return []

    monkeypatch.setattr(browser_api_module, "list_images_by_source", fake_list_by_source)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "image", "list", "--source", "all"])
    assert result.exit_code == 0

    payload = _json_data(result.output)
    assert len(payload["items"]) == 2
    assert payload["warnings"] == ["public image catalog unavailable."]
    _assert_compact_public_payload(payload)
    assert "/Users/alice/private.log" not in result.output
    assert "img-secret-123" not in result.output
    assert "socket hang up" not in result.output


def test_image_list_all_sources_all_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    monkeypatch.setattr(
        browser_api_module,
        "list_images_by_source",
        lambda source="official", session=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "image", "list", "--source", "all"])
    assert result.exit_code != 0
    assert "Image catalog is unavailable." in result.output
    assert "boom" not in result.output


def test_image_name_resolver_uses_successful_sources_when_others_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _cacheable_image_session()
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _image_scope()
    index.upsert(
        scope,
        [ResourceIdentity(resource_id="img-old", name="target:v1")],
    )
    monkeypatch.setattr(
        ResourceIndex,
        "for_account",
        classmethod(lambda cls, account=None: index),
    )

    def fake_list_by_source(source="official", session=None):
        if source != "private":
            raise RuntimeError(f"{source} unavailable")
        return [
            CustomImageInfo(
                image_id="img-new",
                url="registry/target:v1",
                name="target",
                framework="",
                version="v1",
                source="SOURCE_PRIVATE",
                status="READY",
                description="",
                created_at="",
            )
        ]

    monkeypatch.setattr(browser_api_module, "list_images_by_source", fake_list_by_source)

    resolved = _REAL_RESOLVE_IMAGE_NAME(
        Context(),
        "target:v1",
        session=session,
        require_live=True,
    )

    assert resolved == "img-new"
    assert [item.resource_id for item in index.lookup(scope, "target:v1")] == [
        "img-new"
    ]
    old = index.lookup_id(scope, "img-old", include_tombstoned=True)
    assert old is not None
    assert old.tombstoned_at is not None


def test_destructive_image_lookup_all_sources_fail_preserves_cached_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    session = _cacheable_image_session()
    monkeypatch.setattr(web_session_module, "get_web_session", lambda: session)
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _image_scope()
    index.upsert(
        scope,
        [ResourceIdentity(resource_id="img-old", name="target:v1")],
    )
    monkeypatch.setattr(
        ResourceIndex,
        "for_account",
        classmethod(lambda cls, account=None: index),
    )
    monkeypatch.setattr(
        image_commands_module,
        "_resolve_image_name",
        _REAL_RESOLVE_IMAGE_NAME,
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_images_by_source",
        lambda source="official", session=None: (_ for _ in ()).throw(
            RuntimeError(f"{source} endpoint unavailable")
        ),
    )
    monkeypatch.setattr(
        browser_api_module,
        "delete_image",
        lambda **_: pytest.fail("delete must not run after catalogue failure"),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "image", "delete", "target:v1", "--yes"],
    )

    assert result.exit_code != 0
    assert "APIError" in result.output
    assert "Image catalog is unavailable." in result.output
    assert "endpoint unavailable" not in result.output
    assert [item.resource_id for item in index.lookup(scope, "target:v1")] == [
        "img-old"
    ]
    old = index.lookup_id(scope, "img-old", include_tombstoned=True)
    assert old is not None
    assert old.tombstoned_at is None


def test_image_detail_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    _patch_image_name_resolver(monkeypatch, {"detail-img:2.0": "img-123"})

    monkeypatch.setattr(
        browser_api_module,
        "get_image_detail",
        lambda image_id, session=None: browser_api_module.CustomImageInfo(
            image_id=image_id,
            url="registry/detail-img",
            name="detail-img",
            framework="pytorch",
            version="2.0",
            source="SOURCE_PRIVATE",
            status="READY",
            description="Detailed",
            created_at="2026-01-15",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "image", "detail", "detail-img:2.0"])
    assert result.exit_code == 0

    payload = _json_data(result.output)
    assert payload == {
        "name": "detail-img:2.0",
        "status": "READY",
        "framework": "pytorch",
        "visibility": "private",
    }
    _assert_compact_public_payload(payload)
    assert "img-123" not in result.output
    assert "registry/detail-img" not in result.output
    assert "Detailed" not in result.output
    assert "2026-01-15" not in result.output


def test_image_detail_human(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    _patch_image_name_resolver(monkeypatch, {"detail-img:2.0": "img-123"})

    monkeypatch.setattr(
        browser_api_module,
        "get_image_detail",
        lambda image_id, session=None: browser_api_module.CustomImageInfo(
            image_id=image_id,
            url="registry/detail-img",
            name="detail-img",
            framework="pytorch",
            version="2.0",
            source="SOURCE_PRIVATE",
            status="READY",
            description="Detailed",
            created_at="2026-01-15",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["image", "detail", "detail-img:2.0"])
    assert result.exit_code == 0
    assert result.output.startswith("Name: detail-img:2.0\n")
    assert "Image Detail" not in result.output
    assert "Source:" not in result.output
    assert "img-123" not in result.output
    assert "Registry:" not in result.output
    assert "Description:" not in result.output
    assert "Created:" not in result.output
    assert "registry/detail-img" not in result.output
    assert "Detailed" not in result.output
    assert "2026-01-15" not in result.output


def test_image_detail_retries_stale_cached_handle_by_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    resolve_calls: list[bool] = []
    detail_calls: list[str] = []
    invalidated: list[str] = []

    def _resolve(
        _ctx,
        _name,
        *,
        require_live=False,
        **_kwargs,
    ):
        resolve_calls.append(require_live)
        return "img-new" if require_live else "img-old"

    monkeypatch.setattr(image_commands_module, "_resolve_image_name", _resolve)
    monkeypatch.setattr(
        image_commands_module,
        "forget_resource_identity",
        lambda **kwargs: invalidated.append(kwargs["resource_id"]),
    )

    def _detail(image_id, session=None):
        detail_calls.append(image_id)
        if image_id == "img-old":
            raise RuntimeError("404 image not found")
        return browser_api_module.CustomImageInfo(
            image_id=image_id,
            url="registry/detail-img",
            name="detail-img",
            framework="pytorch",
            version="2.0",
            source="SOURCE_PRIVATE",
            status="READY",
            description="Detailed",
            created_at="2026-01-15",
        )

    monkeypatch.setattr(browser_api_module, "get_image_detail", _detail)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "image", "detail", "detail-img:2.0"],
    )

    assert result.exit_code == 0, result.output
    assert resolve_calls == [False, True]
    assert detail_calls == ["img-old", "img-new"]
    assert invalidated == ["img-old"]
    assert "img-old" not in result.output
    assert "img-new" not in result.output


def test_image_detail_error_hides_internal_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    _patch_image_name_resolver(monkeypatch, {"detail-img:2.0": "img-secret-123"})
    monkeypatch.setattr(
        browser_api_module,
        "get_image_detail",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "request payload failed at /Users/alice/private.log "
                "image_id=img-secret-123"
            )
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "image", "detail", "detail-img:2.0"],
    )

    assert result.exit_code != 0
    _assert_safe_failure(result.output, "Could not load image details.")


def test_image_register_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    captured: dict[str, Any] = {}

    def fake_create_image(
        name,
        version,
        workspace_id=None,
        description="",
        visibility="VISIBILITY_PRIVATE",
        add_method=0,
        session=None,
    ) -> dict:
        captured["name"] = name
        captured["version"] = version
        captured["add_method"] = add_method
        return {"image": {"image_id": "img-new-001", "address": "registry.example/img-new-001"}}

    monkeypatch.setattr(browser_api_module, "create_image", fake_create_image)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "image", "register", "-n", "my-img", "-v", "v1.0", "--method", "address"],
    )
    assert result.exit_code == 0

    payload = _json_data(result.output)
    assert payload == {
        "name": "my-img:v1.0",
        "status": "registered",
    }
    _assert_compact_public_payload(payload)
    assert "img-new-001" not in result.output
    assert captured["name"] == "my-img"
    assert captured["version"] == "v1.0"
    assert captured["add_method"] == 2


def test_image_register_human_push(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    monkeypatch.setattr(
        browser_api_module,
        "create_image",
        lambda name, version, workspace_id=None, description="", visibility="VISIBILITY_PRIVATE", add_method=0, session=None: {
            "image": {"image_id": "img-new-002", "address": "registry.example/my-img:v0.1"}
        },
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["image", "register", "-n", "test", "-v", "v0.1"])
    assert result.exit_code == 0
    assert result.output.splitlines()[0] == "OK Image registered: test:v0.1"
    assert "img-new-002" not in result.output
    assert "docker tag" in result.output
    assert "docker push" in result.output
    assert "registry.example/my-img:v0.1" in result.output


def test_image_register_json_push_keeps_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    monkeypatch.setattr(
        browser_api_module,
        "create_image",
        lambda **_kwargs: {
            "image": {
                "image_id": "img-new-003",
                "address": "registry.example/my-img:v0.2",
            }
        },
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "image", "register", "-n", "test", "-v", "v0.2"],
    )

    assert result.exit_code == 0, result.output
    assert _json_data(result.output) == {
        "name": "test:v0.2",
        "status": "registered",
        "registry": "registry.example/my-img:v0.2",
    }
    assert "img-new-003" not in result.output


def test_image_register_errors_hide_internal_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    monkeypatch.setattr(
        browser_api_module,
        "create_image",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "request payload failed at /Users/alice/private.log "
                "image_id=img-secret-123"
            )
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "image", "register", "-n", "test", "-v", "v0.2"],
    )

    assert result.exit_code != 0
    _assert_safe_failure(result.output, "Could not register image.")


def test_image_register_wait_error_hides_internal_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    monkeypatch.setattr(
        browser_api_module,
        "create_image",
        lambda **_kwargs: {"image": {"image_id": "img-secret-123"}},
    )
    monkeypatch.setattr(
        browser_api_module,
        "wait_for_image_ready",
        lambda **_kwargs: (_ for _ in ()).throw(
            TimeoutError(
                "request payload failed at /Users/alice/private.log "
                "image_id=img-secret-123"
            )
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "image",
            "register",
            "-n",
            "test",
            "-v",
            "v0.2",
            "--wait",
        ],
    )

    assert result.exit_code != 0
    _assert_safe_failure(result.output, "Image did not become ready.")


def test_image_save_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    captured: dict[str, Any] = {}

    def fake_save(
        notebook_id, name, version="v1", description="", session=None
    ) -> dict:
        captured["notebook_id"] = notebook_id
        captured["name"] = name
        return {"image": {"image_id": "img-saved-001"}}

    monkeypatch.setattr(browser_api_module, "save_notebook_as_image", fake_save)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--json",
            "image",
            "save",
            "demo-notebook",
            "--workspace",
            "Test Workspace",
            "-n",
            "saved-img",
        ],
    )
    assert result.exit_code == 0

    payload = _json_data(result.output)
    assert payload == {"name": "saved-img:v1", "status": "saving"}
    _assert_compact_public_payload(payload)
    assert "img-saved-001" not in result.output
    assert captured["notebook_id"] == "notebook-abc"


def test_image_save_forwards_pick_to_notebook_name_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    seen: dict[str, object] = {}

    def resolve_notebook(*_args, **kwargs):  # noqa: ANN001
        seen["pick"] = kwargs["pick"]
        return "notebook-abc", None

    monkeypatch.setattr(
        notebook_lookup_module,
        "_resolve_notebook_id",
        resolve_notebook,
    )
    monkeypatch.setattr(
        browser_api_module,
        "save_notebook_as_image",
        lambda notebook_id, name, version="v1", description="", session=None: {
            "image": {"image_id": "img-saved-002"}
        },
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "image",
            "save",
            "demo-notebook",
            "--workspace",
            "Test Workspace",
            "--pick",
            "2",
            "-n",
            "saved-img",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == {"pick": 2}
    assert "img-saved-002" not in result.output


def test_image_save_public_visibility_calls_update_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    update_captured: dict[str, Any] = {}

    def fake_save(
        notebook_id, name, version="v1", description="", session=None
    ) -> dict:
        return {"image": {"image_id": "img-pub-001"}}

    def fake_update(image_id, *, visibility=None, description=None, session=None) -> dict:
        update_captured["image_id"] = image_id
        update_captured["visibility"] = visibility
        return {}

    monkeypatch.setattr(browser_api_module, "save_notebook_as_image", fake_save)
    monkeypatch.setattr(browser_api_module, "update_image", fake_update)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "image",
            "save",
            "demo-notebook",
            "--workspace",
            "Test Workspace",
            "-n",
            "shared-base",
            "--visibility",
            "public",
        ],
    )
    assert result.exit_code == 0
    assert update_captured == {"image_id": "img-pub-001", "visibility": "VISIBILITY_PUBLIC"}
    assert result.output == "OK Image saving: shared-base:v1\n"


def test_image_save_private_visibility_calls_update_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    seen_visibility: dict[str, Any] = {}

    def fake_save(
        notebook_id, name, version="v1", description="", session=None
    ) -> dict:
        return {"image": {"image_id": "img-priv-001"}}

    def fake_update(image_id, *, visibility=None, description=None, session=None) -> dict:
        seen_visibility["visibility"] = visibility
        return {}

    monkeypatch.setattr(browser_api_module, "save_notebook_as_image", fake_save)
    monkeypatch.setattr(browser_api_module, "update_image", fake_update)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "image",
            "save",
            "demo-notebook",
            "--workspace",
            "Test Workspace",
            "-n",
            "my-img",
            "--visibility",
            "private",
        ],
    )
    assert result.exit_code == 0
    assert seen_visibility == {"visibility": "VISIBILITY_PRIVATE"}
    assert result.output == "OK Image saving: my-img:v1\n"


def test_image_save_visibility_warning_is_compact_and_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    monkeypatch.setattr(
        browser_api_module,
        "save_notebook_as_image",
        lambda **_kwargs: {"image": {"image_id": "img-saved-warning"}},
    )
    monkeypatch.setattr(
        browser_api_module,
        "update_image",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "failed at /Users/alice/private.log image_id=img-saved-warning"
            )
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "image",
            "save",
            "demo-notebook",
            "--workspace",
            "Test Workspace",
            "-n",
            "saved-img",
            "--visibility",
            "public",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _json_data(result.output) == {
        "name": "saved-img:v1",
        "status": "saving",
        "warning": (
            "Visibility was not updated. Retry with: "
            "inspire image set-visibility saved-img:v1 --visibility public"
        ),
    }
    assert "/Users/alice/private.log" not in result.output
    assert "img-saved-warning" not in result.output


def test_image_save_error_hides_internal_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    monkeypatch.setattr(
        browser_api_module,
        "save_notebook_as_image",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "request payload failed at /Users/alice/private.log "
                "image_id=img-secret-123"
            )
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "image",
            "save",
            "demo-notebook",
            "--workspace",
            "Test Workspace",
            "-n",
            "saved-img",
        ],
    )

    assert result.exit_code != 0
    _assert_safe_failure(result.output, "Could not save notebook as an image.")


def test_image_set_visibility_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    _patch_image_name_resolver(monkeypatch, {"my-image:v1": "image-abc-def"})
    monkeypatch.setattr(
        image_commands_module,
        "require_confirmation",
        lambda *_args, **_kwargs: pytest.fail(
            "set-visibility must not use deletion confirmation"
        ),
    )

    captured: dict[str, Any] = {}

    def fake_update(image_id, *, visibility=None, description=None, session=None) -> dict:
        captured["image_id"] = image_id
        captured["visibility"] = visibility
        return {"ok": True}

    monkeypatch.setattr(browser_api_module, "update_image", fake_update)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["image", "set-visibility", "my-image:v1", "--visibility", "public"],
    )
    assert result.exit_code == 0
    assert captured == {"image_id": "image-abc-def", "visibility": "VISIBILITY_PUBLIC"}
    assert result.output == "OK Image updated: my-image:v1\n"
    assert "image-abc-def" not in result.output

    result2 = runner.invoke(
        cli_main,
        ["image", "set-visibility", "my-image:v1", "--visibility", "private"],
    )
    assert result2.exit_code == 0
    assert captured == {"image_id": "image-abc-def", "visibility": "VISIBILITY_PRIVATE"}
    assert result2.output == "OK Image updated: my-image:v1\n"
    assert "image-abc-def" not in result2.output


def test_image_set_visibility_forwards_pick_to_name_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    seen: dict[str, object] = {}

    def resolve_image(*_args, **kwargs):  # noqa: ANN001
        seen["pick"] = kwargs["pick"]
        return "image-abc-def"

    monkeypatch.setattr(image_commands_module, "_resolve_image_name", resolve_image)
    monkeypatch.setattr(
        browser_api_module,
        "update_image",
        lambda **_kwargs: {"ok": True},
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "image",
            "set-visibility",
            "my-image:v1",
            "--pick",
            "2",
            "--visibility",
            "public",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == {"pick": 2}
    assert _json_data(result.output) == {
        "name": "my-image:v1",
        "status": "updated",
    }
    assert "image-abc-def" not in result.output


def test_image_set_visibility_error_hides_internal_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    _patch_image_name_resolver(monkeypatch, {"my-image:v1": "img-secret-123"})
    monkeypatch.setattr(
        browser_api_module,
        "update_image",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "request payload failed at /Users/alice/private.log "
                "image_id=img-secret-123"
            )
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "image",
            "set-visibility",
            "my-image:v1",
            "--visibility",
            "public",
        ],
    )

    assert result.exit_code != 0
    _assert_safe_failure(result.output, "Could not update image visibility.")


def test_image_save_fallback_resolves_image_id_via_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    monkeypatch.setattr(
        browser_api_module,
        "save_notebook_as_image",
        lambda notebook_id, name, version="v1", description="", session=None: {"image": {}},
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_images_by_source",
        lambda source="official", session=None: [
            CustomImageInfo(
                image_id="img-older",
                url="registry/saved-img:v1",
                name="saved-img",
                framework="",
                version="v1",
                source="SOURCE_PRIVATE",
                status="READY",
                description="",
                created_at="2026-04-20T00:00:00Z",
            ),
            CustomImageInfo(
                image_id="img-newest",
                url="registry/saved-img:v1",
                name="saved-img",
                framework="",
                version="v1",
                source="SOURCE_PRIVATE",
                status="BUILDING",
                description="",
                created_at="2026-04-22T00:00:00Z",
            ),
            CustomImageInfo(
                image_id="img-other",
                url="registry/other:v1",
                name="other",
                framework="",
                version="v1",
                source="SOURCE_PRIVATE",
                status="READY",
                description="",
                created_at="2026-04-22T01:00:00Z",
            ),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "image",
            "save",
            "demo-notebook",
            "--workspace",
            "Test Workspace",
            "-n",
            "saved-img",
        ],
    )

    assert result.exit_code == 0
    assert result.output == "OK Image saving: saved-img:v1\n"
    assert "img-newest" not in result.output


def test_image_save_unknown_when_fallback_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    monkeypatch.setattr(
        browser_api_module,
        "save_notebook_as_image",
        lambda notebook_id, name, version="v1", description="", session=None: {"image": {}},
    )

    def _raise(source="official", session=None):
        raise RuntimeError("list endpoint unreachable")

    monkeypatch.setattr(browser_api_module, "list_images_by_source", _raise)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "image",
            "save",
            "demo-notebook",
            "--workspace",
            "Test Workspace",
            "-n",
            "saved-img",
        ],
    )

    assert result.exit_code == 0
    assert result.output == "OK Image saving: saved-img:v1\n"
    assert "unknown" not in result.output


def test_image_delete_with_yes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    _patch_image_name_resolver(monkeypatch, {"stale-image:v1": "img-del-001"})

    deleted_ids: list[str] = []

    def fake_delete(image_id, session=None) -> dict:
        deleted_ids.append(image_id)
        return {}

    monkeypatch.setattr(browser_api_module, "delete_image", fake_delete)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["image", "delete", "stale-image:v1", "--yes"])
    assert result.exit_code == 0
    assert result.output == "OK Image deleted: stale-image:v1\n"
    assert "img-del-001" not in result.output
    assert deleted_ids == ["img-del-001"]


def test_image_delete_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    _patch_image_name_resolver(monkeypatch, {"stale-image:v2": "img-del-002"})

    monkeypatch.setattr(
        browser_api_module,
        "delete_image",
        lambda image_id, session=None: {},
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "image", "delete", "stale-image:v2", "--yes"])
    assert result.exit_code == 0

    payload = _json_data(result.output)
    # v2: delete output carries the user-facing name, not the internal image_id.
    assert payload["name"] == "stale-image:v2"
    assert payload["status"] == "deleted"
    _assert_compact_public_payload(payload)


def test_image_delete_error_hides_internal_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    _patch_image_name_resolver(monkeypatch, {"stale-image:v2": "img-secret-123"})
    monkeypatch.setattr(
        browser_api_module,
        "delete_image",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "request payload failed at /Users/alice/private.log "
                "image_id=img-secret-123"
            )
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "image", "delete", "stale-image:v2", "--yes"],
    )

    assert result.exit_code != 0
    _assert_safe_failure(result.output, "Could not delete image.")


def test_image_delete_json_requires_confirmation_before_session_or_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        image_commands_module,
        "require_web_session",
        lambda *_args, **_kwargs: calls.append("session"),
    )
    monkeypatch.setattr(
        image_commands_module,
        "_resolve_image_name",
        lambda *_args, **_kwargs: calls.append("resolve"),
    )
    monkeypatch.setattr(
        browser_api_module,
        "delete_image",
        lambda **_kwargs: calls.append("delete"),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "image", "delete", "stale-image:v3"],
    )

    assert result.exit_code == 12, result.output
    assert json.loads(result.output) == {
        "success": False,
        "error": {
            "type": "ConfirmationRequired",
            "code": 12,
            "message": "Image deletion requires confirmation.",
            "hint": "Pass --yes to confirm.",
        },
    }
    assert calls == []


def test_image_delete_prompts_without_yes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        image_commands_module,
        "_resolve_image_name",
        lambda *_args, **_kwargs: calls.append("resolve"),
    )
    monkeypatch.setattr(
        browser_api_module,
        "delete_image",
        lambda **_kwargs: calls.append("delete"),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["image", "delete", "stale-image:v3"], input="n\n")

    assert result.exit_code != 0
    assert "Aborted!" in result.output
    assert calls == []


def test_image_save_workspace_metavar_is_name_only() -> None:
    result = CliRunner().invoke(cli_main, ["image", "save", "--help"])

    assert result.exit_code == 0, result.output
    assert "--workspace NAME" in result.output
    assert "--workspace NAME|all" not in result.output
    assert "--workspace TEXT" not in result.output


# ---------------------------------------------------------------------------
# wait_for_image_ready tests
# ---------------------------------------------------------------------------


def test_wait_for_image_ready_returns_on_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from inspire.platform.web.browser_api import images as images_module

    call_count = 0

    def fake_get_image_detail(image_id, session=None):
        nonlocal call_count
        call_count += 1
        return CustomImageInfo(
            image_id=image_id,
            url="",
            name="test",
            framework="",
            version="",
            source="SOURCE_PRIVATE",
            status="READY",
            description="",
            created_at="",
        )

    monkeypatch.setattr(images_module, "get_image_detail", fake_get_image_detail)
    monkeypatch.setattr(images_module, "get_web_session", lambda: FakeWebSession())

    result = images_module.wait_for_image_ready("img-001", session=FakeWebSession())
    assert result.status == "READY"
    assert call_count == 1


def test_wait_for_image_ready_accepts_success_from_mirror_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platform returns `SUCCESS` (not `READY`) for images produced by
    ``inspire image save``. Both must resolve the wait; this test pins that
    behaviour so future refactors don't drop one state alias."""
    from inspire.platform.web.browser_api import images as images_module

    def fake_get_image_detail(image_id, session=None):
        return CustomImageInfo(
            image_id=image_id,
            url="docker.x/y:v2",
            name="y:v2",
            framework="",
            version="v2",
            source="SOURCE_PUBLIC",
            status="SUCCESS",
            description="",
            created_at="",
        )

    monkeypatch.setattr(images_module, "get_image_detail", fake_get_image_detail)
    monkeypatch.setattr(images_module, "get_web_session", lambda: FakeWebSession())

    result = images_module.wait_for_image_ready("img-save", session=FakeWebSession())
    assert result.status == "SUCCESS"


def test_wait_for_image_ready_raises_on_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    from inspire.platform.web.browser_api import images as images_module

    def fake_get_image_detail(image_id, session=None):
        return CustomImageInfo(
            image_id=image_id,
            url="",
            name="test",
            framework="",
            version="",
            source="SOURCE_PRIVATE",
            status="FAILED",
            description="",
            created_at="",
        )

    monkeypatch.setattr(images_module, "get_image_detail", fake_get_image_detail)

    with pytest.raises(ValueError, match="build failed"):
        images_module.wait_for_image_ready("img-002", session=FakeWebSession())


def test_wait_for_image_ready_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    from inspire.platform.web.browser_api import images as images_module

    calls: list[int] = []

    def fake_time():
        calls.append(1)
        return 0 if len(calls) == 1 else 999

    monkeypatch.setattr(images_module.time, "time", fake_time)
    monkeypatch.setattr(images_module.time, "sleep", lambda s: None)

    def fake_get_image_detail(image_id, session=None):
        return CustomImageInfo(
            image_id=image_id,
            url="",
            name="test",
            framework="",
            version="",
            source="SOURCE_PRIVATE",
            status="BUILDING",
            description="",
            created_at="",
        )

    monkeypatch.setattr(images_module, "get_image_detail", fake_get_image_detail)

    with pytest.raises(TimeoutError, match="did not reach a terminal success state"):
        images_module.wait_for_image_ready("img-003", session=FakeWebSession(), timeout=10)


# ---------------------------------------------------------------------------
# Formatter tests
# ---------------------------------------------------------------------------


def test_format_image_list_empty():
    result = image_commands_module._format_image_list([])
    assert "No images found" in result


def test_format_image_list_with_items():
    images = [
        {
            "name": "pytorch:2.0",
            "framework": "PyTorch",
            "visibility": "official",
            "status": "READY",
        },
        {
            "name": "custom:1.5",
            "framework": "PT",
            "visibility": "private",
            "status": "BUILDING",
        },
    ]
    result = image_commands_module._format_image_list(images)
    assert "pytorch" in result
    assert "custom" in result
    assert "Total:" not in result
    assert "Visibility" in result
    assert "Source" not in result
    assert "official" in result
    assert "private" in result
    assert "READY" in result
    assert "BUILDING" in result


def test_format_image_detail():
    data = {
        "name": "my-image:2.0",
        "framework": "pytorch",
        "visibility": "private",
        "status": "READY",
        "registry": "registry/my-image:v1",
        "description": "Test image",
        "created_at": "2026-01-15",
    }
    result = image_commands_module._format_image_detail(data)
    assert "Image Detail" not in result
    assert "my-image" in result
    assert "img-123" not in result
    assert "private" in result
    assert "Source" not in result
    assert "READY" in result
    assert "Registry" not in result
    assert "registry/my-image:v1" not in result
    assert "Description" not in result
    assert "Test image" not in result
    assert "Created" not in result
    assert "2026-01-15" not in result
