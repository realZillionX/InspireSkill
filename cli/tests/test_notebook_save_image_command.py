"""Tests for `inspire notebook save-image`.

The command commits a running notebook into a custom image, so its assertions
are about the notebook it locks and the image label it reports — never the
handles behind either.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.notebook import notebook_lookup as notebook_lookup_module
from inspire.cli.main import main as cli_main
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.platform.web.browser_api.images import CustomImageInfo


_FORBIDDEN_PUBLIC_KEYS = {
    "id",
    "image_id",
    "raw",
    "payload",
    "result",
    "scanned",
    "source",
}


class FakeWebSession:
    workspace_id = "ws-test-workspace"
    all_workspace_ids = ["ws-test-workspace"]
    all_workspace_names = {"ws-test-workspace": "Test Workspace"}
    storage_state: dict = {}


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


def _patch_config_and_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> config_module.Config:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        path_aliases={"me": str(tmp_path / "logs")},
    )

    def fake_from_files_and_env(cls, require_credentials: bool = True) -> tuple:
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


def test_save_image_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
            "notebook",
            "save-image",
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


def test_save_image_forwards_pick_to_notebook_name_resolution(
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
            "notebook",
            "save-image",
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


def test_save_image_public_visibility_calls_update_image(
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
            "notebook",
            "save-image",
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


def test_save_image_private_visibility_calls_update_image(
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
            "notebook",
            "save-image",
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


def test_save_image_visibility_warning_is_compact_and_safe(
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
            "notebook",
            "save-image",
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


def test_save_image_error_hides_internal_failure(
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
            "notebook",
            "save-image",
            "demo-notebook",
            "--workspace",
            "Test Workspace",
            "-n",
            "saved-img",
        ],
    )

    assert result.exit_code != 0
    _assert_safe_failure(result.output, "Could not save notebook as an image.")


def test_save_image_fallback_resolves_image_id_via_list(
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
            "notebook",
            "save-image",
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


def test_save_image_unknown_when_fallback_fails(
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
            "notebook",
            "save-image",
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


def test_save_image_workspace_metavar_is_name_only() -> None:
    result = CliRunner().invoke(cli_main, ["notebook", "save-image", "--help"])

    assert result.exit_code == 0, result.output
    assert "--workspace NAME" in result.output
    assert "--workspace NAME|all" not in result.output
    assert "--workspace TEXT" not in result.output


def test_save_image_moved_off_the_image_group() -> None:
    """The migration left no alias behind on `inspire image`."""
    result = CliRunner().invoke(cli_main, ["image", "save", "--help"])

    assert result.exit_code != 0
    assert "No such command" in result.output
