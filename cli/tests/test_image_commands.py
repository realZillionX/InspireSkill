"""Tests for image management commands and API functions."""

import json
import threading
from pathlib import Path
from typing import Any, Optional

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.image import image_commands as image_commands_module
from inspire.cli.context import Context
from inspire.cli.main import main as cli_main
from inspire.cli.utils.resource_index import (
    ResourceIdentity,
    ResourceIndex,
    ResourceScope,
)
from inspire.config import workspaces as workspaces_module
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.platform.web.browser_api.images import (
    CustomImageInfo,
    _image_from_api,
    list_images_by_source,
)


# Images live in a per-workspace registry, so every image command takes the
# workspace that registry belongs to. `_OTHER_WS` is deliberately *not* the
# session's active workspace.
_WS = ["--workspace", "Test Workspace"]
_OTHER_WS = ["--workspace", "Other Workspace"]

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
    # The session's own workspace is "Test Workspace"; "Other Workspace" is the
    # one the session is *not* pointed at, which is what --workspace has to
    # reach for the registry to be addressable at all.
    workspace_id = "ws-test-workspace"
    all_workspace_ids = ["ws-test-workspace", "ws-other-workspace"]
    all_workspace_names = {
        "ws-test-workspace": "Test Workspace",
        "ws-other-workspace": "Other Workspace",
    }
    storage_state = {}


@pytest.fixture(autouse=True)
def _offline_workspace_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve workspace names from the session alone, never over the network."""
    monkeypatch.setattr(
        workspaces_module,
        "_enumerated_workspace_names",
        lambda _session: {},
    )


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
    return config


def _patch_image_candidates(
    monkeypatch: pytest.MonkeyPatch, *images: CustomImageInfo
) -> None:
    monkeypatch.setattr(
        browser_api_module,
        "list_images_by_source",
        lambda source="official", session=None, workspace_id=None: list(images),
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

    def fake_image_v2(session, action: str, body: Optional[dict] = None, *, timeout: int = 30) -> Any:
        captured["action"] = action
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

    monkeypatch.setattr(images_module, "_image_v2", fake_image_v2)
    monkeypatch.setattr(
        images_module,
        "_get_session_and_workspace_id",
        lambda workspace_id, session: (FakeWebSession(), "ws-test"),
    )

    results = list_images_by_source(source="official")
    assert captured["action"] == "ListImages"
    assert len(results) == 1
    assert results[0].image_id == "img-off-001"
    assert results[0].source == "SOURCE_OFFICIAL"
    assert results[0].status == "READY"
    assert captured["body"]["filter"]["source"] == "SOURCE_OFFICIAL"


def test_list_images_by_source_public(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def fake_image_v2(session, action: str, body: Optional[dict] = None, *, timeout: int = 30) -> Any:
        captured["action"] = action
        captured["body"] = body
        return {"images": []}

    from inspire.platform.web.browser_api import images as images_module

    monkeypatch.setattr(images_module, "_image_v2", fake_image_v2)
    monkeypatch.setattr(
        images_module,
        "_get_session_and_workspace_id",
        lambda workspace_id, session: (FakeWebSession(), "ws-test"),
    )

    results = list_images_by_source(source="public")
    assert captured["action"] == "ListImages"
    assert results == []
    # Public uses source_list + visibility filter
    assert captured["body"]["filter"]["visibility"] == "VISIBILITY_PUBLIC"
    assert "SOURCE_PUBLIC" in captured["body"]["filter"]["source_list"]


def test_list_images_by_source_private_personal_visible(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def fake_image_v2(session, action: str, body: Optional[dict] = None, *, timeout: int = 30) -> Any:
        captured["action"] = action
        captured["body"] = body
        return {"images": []}

    from inspire.platform.web.browser_api import images as images_module

    monkeypatch.setattr(images_module, "_image_v2", fake_image_v2)
    monkeypatch.setattr(
        images_module,
        "_get_session_and_workspace_id",
        lambda workspace_id, session: (FakeWebSession(), "ws-test"),
    )

    results = list_images_by_source(source="private")
    assert captured["action"] == "ListImages"
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
    assert "set-visibility" in result.output
    assert "delete" in result.output


@pytest.mark.parametrize(
    "command",
    [
        ["list"],
        ["detail"],
        ["register"],
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
    # There is one flow the CLI can drive: reserve a slot, then you push.
    assert "docker-push" in result.output
    assert "stays FAILED until that push" in result.output
    # The file-upload route needs an upload this CLI does not implement, so it
    # is described as web-only rather than offered as a mode that always errors.
    assert "--method" not in result.output


# ---------------------------------------------------------------------------
# Registry workspace scoping
# ---------------------------------------------------------------------------

# Every image command addresses one workspace's registry, and says so the same
# way: a required, single, name-only --workspace.
_SCOPED_IMAGE_COMMANDS = (
    ["image", "list"],
    ["image", "detail", "some-image:v1"],
    ["image", "register", "-n", "some-image", "-v", "v1"],
    ["image", "set-visibility", "some-image:v1", "--visibility", "public"],
    ["image", "delete", "some-image:v1", "--yes"],
)

_IMAGE_APIS = (
    "list_images_by_source",
    "get_image_detail",
    "create_image",
    "update_image",
    "delete_image",
)


@pytest.mark.parametrize("args", _SCOPED_IMAGE_COMMANDS)
def test_image_commands_share_one_registry_workspace_option(args: list[str]) -> None:
    help_result = CliRunner().invoke(cli_main, [*args[:2], "--help"])

    assert help_result.exit_code == 0, help_result.output
    assert "--workspace NAME" in help_result.output
    assert "image registry" in " ".join(help_result.output.split())

    missing = CliRunner().invoke(cli_main, args)

    assert missing.exit_code == 2, missing.output
    assert "Missing option '--workspace'" in missing.output


@pytest.mark.parametrize("args", _SCOPED_IMAGE_COMMANDS)
def test_image_commands_reject_an_unknown_workspace(
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    for api in _IMAGE_APIS:
        monkeypatch.setattr(
            browser_api_module,
            api,
            lambda **_kwargs: pytest.fail("no image request may run for an unknown workspace"),
        )

    result = CliRunner().invoke(
        cli_main,
        ["--json", *args, "--workspace", "No Such Workspace"],
    )

    assert result.exit_code == 10, result.output
    error = json.loads(result.output)["error"]
    assert error["type"] == "ConfigError"
    assert "Unknown workspace name" in error["message"]


@pytest.mark.parametrize("args", _SCOPED_IMAGE_COMMANDS)
def test_image_commands_reject_workspace_fanout(
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A registry is a single workspace; `all` is not a registry."""
    _patch_config_and_session(monkeypatch, tmp_path)
    for api in _IMAGE_APIS:
        monkeypatch.setattr(
            browser_api_module,
            api,
            lambda **_kwargs: pytest.fail("no image request may run for a fanout workspace"),
        )

    result = CliRunner().invoke(cli_main, ["--json", *args, "--workspace", "all"])

    assert result.exit_code == 10, result.output
    error = json.loads(result.output)["error"]
    assert error["type"] == "ConfigError"
    assert "one workspace name" in error["message"]


def test_image_list_reads_the_requested_workspace_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two workspaces list two different catalogs from the same session.

    This is the `notebook save-image --workspace X` round trip: without the
    scope, an image stored in X is invisible whenever the session's active
    workspace is something else.
    """
    _patch_config_and_session(monkeypatch, tmp_path)

    catalog = {
        "ws-test-workspace": "in-test-ws",
        "ws-other-workspace": "in-other-ws",
    }
    seen: list[str] = []

    def fake_list_by_source(source="official", session=None, workspace_id=None):
        workspace_id = str(workspace_id or "")
        if workspace_id not in seen:
            seen.append(workspace_id)
        if source != "private":
            return []
        return [
            browser_api_module.CustomImageInfo(
                image_id=f"img-{workspace_id}",
                url=f"registry/{catalog[workspace_id]}:v1",
                name=catalog[workspace_id],
                framework="PT",
                version="v1",
                source="SOURCE_PRIVATE",
                status="READY",
                description="",
                created_at="",
            )
        ]

    monkeypatch.setattr(browser_api_module, "list_images_by_source", fake_list_by_source)

    runner = CliRunner()
    session_workspace = runner.invoke(
        cli_main, ["--json", "image", "list", *_WS, "--source", "private"]
    )
    other_workspace = runner.invoke(
        cli_main, ["--json", "image", "list", *_OTHER_WS, "--source", "private"]
    )

    assert session_workspace.exit_code == 0, session_workspace.output
    assert other_workspace.exit_code == 0, other_workspace.output
    assert seen == ["ws-test-workspace", "ws-other-workspace"]
    assert [
        item["name"] for item in _json_data(session_workspace.output)["items"]
    ] == ["in-test-ws:v1"]
    assert [
        item["name"] for item in _json_data(other_workspace.output)["items"]
    ] == ["in-other-ws:v1"]


def _serve_only_from_other_workspace(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Publish ``in-other-ws:v1`` in the "Other Workspace" registry only."""
    # The suite-wide resolver short circuit has to step aside: which registry
    # the name is looked up in is exactly what is under test here.
    monkeypatch.setattr(
        image_commands_module,
        "_resolve_image_name",
        _REAL_RESOLVE_IMAGE_NAME,
    )
    seen: list[str] = []

    def fake_list_by_source(source="official", session=None, workspace_id=None):
        workspace_id = str(workspace_id or "")
        if workspace_id not in seen:
            seen.append(workspace_id)
        if workspace_id != "ws-other-workspace" or source != "private":
            return []
        return [
            CustomImageInfo(
                image_id="img-other",
                url="registry/in-other-ws:v1",
                name="in-other-ws",
                framework="PT",
                version="v1",
                source="SOURCE_PRIVATE",
                status="READY",
                description="",
                created_at="",
            )
        ]

    monkeypatch.setattr(browser_api_module, "list_images_by_source", fake_list_by_source)
    monkeypatch.setattr(
        browser_api_module,
        "get_image_detail",
        lambda image_id, session=None: CustomImageInfo(
            image_id=image_id,
            url="registry/in-other-ws:v1",
            name="in-other-ws",
            framework="PT",
            version="v1",
            source="SOURCE_PRIVATE",
            status="READY",
            description="",
            created_at="",
        ),
    )
    monkeypatch.setattr(browser_api_module, "update_image", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(browser_api_module, "delete_image", lambda **_kwargs: {})
    return seen


@pytest.mark.parametrize(
    "args",
    (
        ["image", "detail", "in-other-ws:v1"],
        ["image", "set-visibility", "in-other-ws:v1", "--visibility", "public"],
        ["image", "delete", "in-other-ws:v1", "--yes"],
    ),
)
def test_image_name_resolution_follows_the_requested_workspace(
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Names resolve against the named registry, not the session's own."""
    _patch_config_and_session(monkeypatch, tmp_path)
    seen = _serve_only_from_other_workspace(monkeypatch)

    found = CliRunner().invoke(cli_main, ["--json", *args, *_OTHER_WS])

    assert found.exit_code == 0, found.output
    assert seen == ["ws-other-workspace"]

    missing = CliRunner().invoke(cli_main, ["--json", *args, *_WS])

    assert missing.exit_code != 0, missing.output
    assert seen == ["ws-other-workspace", "ws-test-workspace"]


def test_image_register_creates_in_the_requested_workspace_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    def fake_create_image(**kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"image": {"image_id": "img-new", "address": "registry.example/my-img:v1"}}

    monkeypatch.setattr(browser_api_module, "create_image", fake_create_image)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "image", "register", "-n", "my-img", *_OTHER_WS, "-v", "v1"],
    )

    assert result.exit_code == 0, result.output
    assert captured["workspace_id"] == "ws-other-workspace"


@pytest.mark.parametrize(
    ("flag", "expected"),
    (
        ([], "VISIBILITY_PRIVATE"),
        (["--visibility", "private"], "VISIBILITY_PRIVATE"),
        (["--visibility", "public"], "VISIBILITY_PUBLIC"),
    ),
)
def test_image_register_maps_visibility_like_set_visibility(
    flag: list[str],
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`register` and `set-visibility` share one visibility mapping."""
    _patch_config_and_session(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    def fake_create_image(**kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"image": {"image_id": "img-new"}}

    monkeypatch.setattr(browser_api_module, "create_image", fake_create_image)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "image", "register", "-n", "my-img", *_WS, "-v", "v1", *flag],
    )

    assert result.exit_code == 0, result.output
    assert captured["visibility"] == expected
    assert captured["visibility"] == image_commands_module._parse_visibility_value(
        flag[-1] if flag else "private"
    )


def test_image_list_human_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    calls: list[str] = []

    def fake_list_by_source(source="official", session=None, workspace_id=None):
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
    result = runner.invoke(cli_main, ["image", "list", *_WS])
    assert result.exit_code == 0
    assert calls == ["official", "public", "project", "private"]
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
        lambda source="official", session=None, workspace_id=None: [
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
    result = runner.invoke(cli_main, ["--json", "image", "list", *_WS])
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
        lambda source="official", session=None, workspace_id=None: [
            browser_api_module.CustomImageInfo(
                image_id="img-priv-001",
                url="registry/my-custom:v1",
                name="personal-visible-img",
                framework="pytorch",
                version="2.1",
                # An image saved from a notebook really does read
                # SOURCE_PUBLIC: `source` is the registry namespace it was
                # built into. Only `visibility` says who can see it.
                source="SOURCE_PUBLIC",
                status="READY",
                description="Custom image",
                created_at="2026-01-10",
                visibility="VISIBILITY_PRIVATE",
            )
        ],
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["--json", "image", "list", *_WS, "--source", "private"]
    )
    assert result.exit_code == 0

    payload = _json_data(result.output)
    assert len(payload["items"]) == 1
    assert payload["items"][0]["name"] == "personal-visible-img:2.1"
    assert payload["items"][0]["visibility"] == "private"
    _assert_compact_public_payload(payload)


def test_image_list_all_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    def fake_list_by_source(source="official", session=None, workspace_id=None):
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
    result = runner.invoke(cli_main, ["--json", "image", "list", *_WS, "--source", "all"])
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

    def fake_list_by_source(source="official", session=None, workspace_id=None):
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
    result = runner.invoke(cli_main, ["--json", "image", "list", *_WS, "--source", "all"])
    assert result.exit_code == 0

    payload = _json_data(result.output)
    assert len(payload["items"]) == 2
    assert payload["warnings"] == ["public image catalog unavailable."]
    _assert_compact_public_payload(payload)
    assert "/Users/alice/private.log" not in result.output
    assert "img-secret-123" not in result.output
    assert "socket hang up" not in result.output


def test_image_list_all_sources_fetches_concurrently_in_stable_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    barrier = threading.Barrier(4)
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_list_by_source(source="official", session=None, workspace_id=None):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        barrier.wait(timeout=2)
        with lock:
            active -= 1
        return [
            browser_api_module.CustomImageInfo(
                image_id=f"img-{source}",
                url=f"registry/{source}",
                name=source,
                framework="",
                version="v1",
                source="SOURCE_OFFICIAL" if source == "official" else "SOURCE_PUBLIC",
                status="READY",
                description="",
                created_at="",
            )
        ]

    monkeypatch.setattr(browser_api_module, "list_images_by_source", fake_list_by_source)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "image", "list", *_WS, "--source", "all", "--all"],
    )

    assert result.exit_code == 0, result.output
    assert max_active == 4
    assert [item["name"] for item in _json_data(result.output)["items"]] == [
        "official:v1",
        "public:v1",
        "project:v1",
        "private:v1",
    ]


def test_image_list_all_sources_all_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    monkeypatch.setattr(
        browser_api_module,
        "list_images_by_source",
        lambda source="official", session=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "image", "list", *_WS, "--source", "all"])
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

    def fake_list_by_source(source="official", session=None, workspace_id=None):
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
        workspace_id="ws-test-workspace",
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
        ["--json", "image", "delete", "target:v1", *_WS, "--yes"],
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
            visibility="VISIBILITY_PRIVATE",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "image", "detail", "detail-img:2.0", *_WS])
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
            visibility="VISIBILITY_PRIVATE",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["image", "detail", "detail-img:2.0", *_WS])
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
        ["--json", "image", "detail", "detail-img:2.0", *_WS],
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
        ["--json", "image", "detail", "detail-img:2.0", *_WS],
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
        return {
            "image": {
                "image_id": "img-new-001",
                "address": "registry.example/inspire-studio/my-img:v1.0",
            }
        }

    monkeypatch.setattr(browser_api_module, "create_image", fake_create_image)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--json",
            "image",
            "register",
            "-n",
            "my-img",
            *_WS,
            "-v",
            "v1.0",
        ],
    )
    assert result.exit_code == 0

    payload = _json_data(result.output)
    assert payload == {
        "name": "my-img:v1.0",
        "status": "awaiting-push",
        "registry": "registry.example/inspire-studio/my-img:v1.0",
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
    result = runner.invoke(
        cli_main, ["image", "register", "-n", "test", *_WS, "-v", "v0.1"]
    )
    assert result.exit_code == 0
    assert result.output.splitlines()[0] == "OK Image slot reserved: test:v0.1"
    assert "img-new-002" not in result.output
    # Login host is derived from the address, so the three lines are usable
    # as-is instead of sending the reader back to the console.
    assert "docker login registry.example" in result.output
    assert "docker tag" in result.output
    assert "docker push" in result.output
    assert "registry.example/my-img:v0.1" in result.output
    assert "stays FAILED until that push completes" in result.output


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
        ["--json", "image", "register", "-n", "test", *_WS, "-v", "v0.2"],
    )

    assert result.exit_code == 0, result.output
    assert _json_data(result.output) == {
        "name": "test:v0.2",
        "status": "awaiting-push",
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
        ["--json", "image", "register", "-n", "test", *_WS, "-v", "v0.2"],
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
            *_WS,
            "-v",
            "v0.2",
            "--wait",
        ],
    )

    assert result.exit_code != 0
    _assert_safe_failure(result.output, "Image did not become ready.")


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
        ["image", "set-visibility", "my-image:v1", *_WS, "--visibility", "public"],
    )
    assert result.exit_code == 0
    assert captured == {"image_id": "image-abc-def", "visibility": "VISIBILITY_PUBLIC"}
    assert result.output == "OK Image updated: my-image:v1\n"
    assert "image-abc-def" not in result.output

    result2 = runner.invoke(
        cli_main,
        ["image", "set-visibility", "my-image:v1", *_WS, "--visibility", "private"],
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
            *_WS,
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
            *_WS,
            "--visibility",
            "public",
        ],
    )

    assert result.exit_code != 0
    _assert_safe_failure(result.output, "Could not update image visibility.")


def test_image_delete_with_yes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    _patch_image_name_resolver(monkeypatch, {"stale-image:v1": "img-del-001"})

    deleted_ids: list[str] = []

    def fake_delete(image_id, session=None) -> dict:
        deleted_ids.append(image_id)
        return {}

    monkeypatch.setattr(browser_api_module, "delete_image", fake_delete)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["image", "delete", "stale-image:v1", *_WS, "--yes"])
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
    result = runner.invoke(
        cli_main, ["--json", "image", "delete", "stale-image:v2", *_WS, "--yes"]
    )
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
        ["--json", "image", "delete", "stale-image:v2", *_WS, "--yes"],
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
        ["--json", "image", "delete", "stale-image:v3", *_WS],
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
    result = runner.invoke(
        cli_main, ["image", "delete", "stale-image:v3", *_WS], input="n\n"
    )

    assert result.exit_code != 0
    assert "Aborted!" in result.output
    assert calls == []


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
    ``inspire notebook save-image``. Both must resolve the wait; this test pins
    that behaviour so future refactors don't drop one state alias."""
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
