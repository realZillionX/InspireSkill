"""Tests for image management commands and API functions."""

import json
from pathlib import Path
from typing import Any, Optional

import pytest
from click.testing import CliRunner

from inspire.cli.main import main as cli_main
from inspire.cli.context import EXIT_VALIDATION_ERROR
from inspire.cli.formatters.human_formatter import format_image_list, format_image_detail
from inspire import config as config_module
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.platform.web.browser_api.images import (
    CustomImageInfo,
    _image_from_api,
    list_images_by_source,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class FakeWebSession:
    workspace_id = "ws-test-workspace"
    storage_state = {}


def _make_config(tmp_path: Path) -> config_module.Config:
    return config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        target_dir=str(tmp_path / "logs"),
        job_cache_path=str(tmp_path / "jobs.json"),
        log_cache_dir=str(tmp_path / "log_cache"),
        job_workspace_id="ws-11111111-1111-1111-1111-111111111111",
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
    )


def _patch_config_and_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> config_module.Config:
    config = _make_config(tmp_path)

    def fake_from_files_and_env(
        cls,
        require_target_dir: bool = False,
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
    return config


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
    assert "set-default" in result.output


def test_image_list_human_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    result = runner.invoke(cli_main, ["image", "list"])
    assert result.exit_code == 0
    assert "pytorch" in result.output
    assert "2.0" in result.output
    assert "official" in result.output
    assert "READY" in result.output
    assert "Total: 1 image(s)" in result.output


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

    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["total"] == 1
    assert payload["data"]["images"][0]["name"] == "pytorch"
    assert payload["data"]["images"][0]["source"] == "SOURCE_OFFICIAL"


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

    payload = json.loads(result.output)
    assert payload["data"]["total"] == 1
    assert payload["data"]["images"][0]["name"] == "personal-visible-img"
    assert payload["data"]["images"][0]["source"] == "SOURCE_PUBLIC"


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

    payload = json.loads(result.output)
    # official + public + private
    assert payload["data"]["total"] == 3
    names = [img["name"] for img in payload["data"]["images"]]
    assert "official-img" in names
    assert "public-img" in names
    assert "personal-visible-img" in names


def test_image_list_all_sources_partial_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    def fake_list_by_source(source="official", session=None):
        if source == "public":
            raise RuntimeError("socket hang up")
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

    payload = json.loads(result.output)
    assert payload["data"]["total"] == 2
    assert len(payload["data"]["warnings"]) == 1
    assert payload["data"]["warnings"][0].startswith("public:")


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


def test_image_detail_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

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
    result = runner.invoke(cli_main, ["--json", "image", "detail", "img-123"])
    assert result.exit_code == 0

    payload = json.loads(result.output)
    assert payload["data"]["image_id"] == "img-123"
    assert payload["data"]["name"] == "detail-img"


def test_image_detail_human(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

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
    result = runner.invoke(cli_main, ["image", "detail", "img-123"])
    assert result.exit_code == 0
    assert "Image Detail" in result.output
    assert "detail-img" in result.output


def test_image_detail_partial_id_resolves_via_private_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    full_image_id = "abcdef12-1111-2222-3333-444444444444"
    called_sources: list[str] = []
    resolved_ids: list[str] = []

    def fake_list_images_by_source(source="official", session=None):
        called_sources.append(source)
        if source == "private":
            return [
                browser_api_module.CustomImageInfo(
                    image_id=full_image_id,
                    url="registry/personal-visible:v1",
                    name="personal-visible-img",
                    framework="pytorch",
                    version="2.1",
                    source="SOURCE_PRIVATE",
                    status="READY",
                    description="",
                    created_at="",
                )
            ]
        return []

    def fake_get_image_detail(image_id, session=None):
        resolved_ids.append(image_id)
        return browser_api_module.CustomImageInfo(
            image_id=image_id,
            url="registry/personal-visible:v1",
            name="personal-visible-img",
            framework="pytorch",
            version="2.1",
            source="SOURCE_PRIVATE",
            status="READY",
            description="",
            created_at="",
        )

    monkeypatch.setattr(browser_api_module, "list_images_by_source", fake_list_images_by_source)
    monkeypatch.setattr(browser_api_module, "get_image_detail", fake_get_image_detail)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "image", "detail", "abcdef12"])
    assert result.exit_code == 0
    assert "private" in called_sources
    assert resolved_ids == [full_image_id]


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

    payload = json.loads(result.output)
    assert payload["data"]["image_id"] == "img-new-001"
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
    assert "Image registered: img-new-002" in result.output
    assert "docker tag" in result.output
    assert "docker push" in result.output
    assert "registry.example/my-img:v0.1" in result.output


def test_image_save_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    captured: dict[str, Any] = {}

    def fake_save(notebook_id, name, version="v1", description="", session=None) -> dict:
        captured["notebook_id"] = notebook_id
        captured["name"] = name
        return {"image": {"image_id": "img-saved-001"}}

    monkeypatch.setattr(browser_api_module, "save_notebook_as_image", fake_save)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "image", "save", "notebook-abc", "-n", "saved-img"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.output)
    assert payload["data"]["image_id"] == "img-saved-001"
    assert captured["notebook_id"] == "notebook-abc"


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
    result = runner.invoke(cli_main, ["image", "save", "notebook-abc", "-n", "saved-img"])

    assert result.exit_code == 0
    assert "Notebook saved as image: img-newest" in result.output


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
    result = runner.invoke(cli_main, ["image", "save", "notebook-abc", "-n", "saved-img"])

    assert result.exit_code == 0
    assert "Notebook saved as image: unknown" in result.output


def test_image_delete_with_force(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    deleted_ids: list[str] = []

    def fake_delete(image_id, session=None) -> dict:
        deleted_ids.append(image_id)
        return {}

    monkeypatch.setattr(browser_api_module, "delete_image", fake_delete)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["image", "delete", "img-del-001", "--force"])
    assert result.exit_code == 0
    assert "img-del-001" in result.output
    assert deleted_ids == ["img-del-001"]


def test_image_delete_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    monkeypatch.setattr(
        browser_api_module,
        "delete_image",
        lambda image_id, session=None: {},
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "image", "delete", "img-del-002", "--force"])
    assert result.exit_code == 0

    payload = json.loads(result.output)
    assert payload["data"]["image_id"] == "img-del-002"
    assert payload["data"]["status"] == "deleted"


def test_image_delete_prompts_without_force(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    monkeypatch.setattr(
        browser_api_module,
        "delete_image",
        lambda image_id, session=None: {},
    )

    runner = CliRunner()
    # Answer 'n' to the confirmation prompt
    result = runner.invoke(cli_main, ["image", "delete", "img-del-003"], input="n\n")
    assert result.exit_code == 0
    assert "Cancelled" in result.output


# ---------------------------------------------------------------------------
# set-default tests
# ---------------------------------------------------------------------------


def test_set_default_job_image(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    # cd to tmp_path so .inspire/config.toml is created there
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["image", "set-default", "--job", "my-pytorch"])
    assert result.exit_code == 0
    assert "job.image" in result.output
    assert "my-pytorch" in result.output

    # Verify the file was written
    config_path = tmp_path / ".inspire" / "config.toml"
    assert config_path.exists()
    content = config_path.read_text()
    assert 'image = "my-pytorch"' in content


def test_set_default_both_images(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["image", "set-default", "--job", "job-img", "--notebook", "nb-img"]
    )
    assert result.exit_code == 0

    config_path = tmp_path / ".inspire" / "config.toml"
    content = config_path.read_text()
    assert "[job]" in content
    assert "[notebook]" in content


def test_set_default_json_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "image", "set-default", "--job", "test-img"])
    assert result.exit_code == 0

    payload = json.loads(result.output)
    assert payload["data"]["updated"]["job.image"] == "test-img"


def test_set_default_requires_at_least_one_option(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["image", "set-default"])
    assert result.exit_code == EXIT_VALIDATION_ERROR


def test_set_default_preserves_existing_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    # Create an existing config
    config_dir = tmp_path / ".inspire"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"
    config_path.write_text('[api]\nbase_url = "https://example.com"\n')

    runner = CliRunner()
    result = runner.invoke(cli_main, ["image", "set-default", "--job", "new-img"])
    assert result.exit_code == 0

    content = config_path.read_text()
    # Both the old and new content should be present
    assert "base_url" in content
    assert "new-img" in content


def test_set_default_from_subdirectory_uses_project_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    config_dir = tmp_path / ".inspire"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"
    config_path.write_text('[auth]\nusername = "test"\n')

    subdir = tmp_path / "src" / "deep"
    subdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(subdir)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["image", "set-default", "--job", "subdir-img"])
    assert result.exit_code == 0

    content = config_path.read_text()
    assert "subdir-img" in content
    assert not (subdir / ".inspire" / "config.toml").exists()


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

    with pytest.raises(TimeoutError, match="did not reach READY"):
        images_module.wait_for_image_ready("img-003", session=FakeWebSession(), timeout=10)


# ---------------------------------------------------------------------------
# Formatter tests
# ---------------------------------------------------------------------------


def test_format_image_list_empty():
    result = format_image_list([])
    assert "No images found" in result


def test_format_image_list_with_items():
    images = [
        {
            "name": "pytorch",
            "framework": "PyTorch",
            "version": "2.0",
            "source": "SOURCE_OFFICIAL",
            "status": "READY",
        },
        {
            "name": "custom",
            "framework": "PT",
            "version": "1.5",
            "source": "SOURCE_PRIVATE",
            "status": "BUILDING",
        },
    ]
    result = format_image_list(images)
    assert "pytorch" in result
    assert "custom" in result
    assert "Total: 2 image(s)" in result
    # Columns should include Version, human-readable source labels
    assert "Version" in result
    assert "official" in result
    assert "private" in result
    assert "READY" in result
    assert "BUILDING" in result


def test_format_image_detail():
    data = {
        "image_id": "img-123",
        "name": "my-image",
        "framework": "pytorch",
        "version": "2.0",
        "source": "SOURCE_PRIVATE",
        "status": "READY",
        "url": "registry/my-image:v1",
        "description": "Test image",
        "created_at": "2026-01-15",
    }
    result = format_image_detail(data)
    assert "Image Detail" in result
    assert "my-image" in result
    assert "img-123" in result
    assert "private" in result  # human-readable source label
    assert "READY" in result
