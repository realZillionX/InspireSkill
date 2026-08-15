"""Tests for `inspire notebook cancel-save-image` and the create-time notebook-name guard.

Both commands sit on Actions that answer a refusal with a message carrying a
raw notebook handle, so the public output is asserted as well as the behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.notebook import notebook_create_flow as flow_module
from inspire.cli.commands.notebook import notebook_lookup as notebook_lookup_module
from inspire.cli.context import Context, EXIT_VALIDATION_ERROR
from inspire.cli.main import main as cli_main
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.cli.utils.quota_resolver import ResolvedQuota


class FakeWebSession:
    workspace_id = "ws-test-workspace"
    all_workspace_ids = ["ws-test-workspace"]
    all_workspace_names = {"ws-test-workspace": "Test Workspace"}
    storage_state: dict = {}


def _json_data(output: str) -> Any:
    parsed = json.loads(output)
    return parsed.get("data", parsed)


def _patch_config_and_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    monkeypatch.setattr(web_session_module, "get_web_session", lambda: FakeWebSession())
    monkeypatch.setattr(
        notebook_lookup_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: ("notebook-abc", None),
    )


# ---------------------------------------------------------------------------
# notebook cancel-save-image
# ---------------------------------------------------------------------------


def test_cancel_save_reports_the_cancelled_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    def fake_cancel(notebook_id, session=None):  # noqa: ANN001
        captured["notebook_id"] = notebook_id
        return True

    monkeypatch.setattr(browser_api_module, "cancel_notebook_image_save", fake_cancel)

    result = CliRunner().invoke(
        cli_main,
        ["notebook", "cancel-save-image", "demo-notebook", "--workspace", "Test Workspace"],
    )

    assert result.exit_code == 0
    assert captured["notebook_id"] == "notebook-abc"
    assert "demo-notebook" in result.output
    # The half-built image survives the cancel; the user has to be told.
    assert "FAILED" in result.output
    assert "notebook-abc" not in result.output


def test_cancel_save_json_distinguishes_nothing_to_cancel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    monkeypatch.setattr(
        browser_api_module,
        "cancel_notebook_image_save",
        lambda notebook_id, session=None: False,
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "notebook",
            "cancel-save-image",
            "demo-notebook",
            "--workspace",
            "Test Workspace",
        ],
    )

    assert result.exit_code == 0
    assert _json_data(result.output) == {
        "notebook": "demo-notebook",
        "status": "not_saving",
    }


def test_cancel_save_human_output_when_nothing_is_saving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    monkeypatch.setattr(
        browser_api_module,
        "cancel_notebook_image_save",
        lambda notebook_id, session=None: False,
    )

    result = CliRunner().invoke(
        cli_main,
        ["notebook", "cancel-save-image", "demo-notebook", "--workspace", "Test Workspace"],
    )

    assert result.exit_code == 0
    assert "No image save is running" in result.output
    assert "FAILED" not in result.output


def test_cancel_save_failure_stays_compact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)

    def _boom(notebook_id, session=None):  # noqa: ANN001
        raise ValueError(
            "API error: Conflict: Save image demo:v1 of notebook notebook-abc ..."
        )

    monkeypatch.setattr(browser_api_module, "cancel_notebook_image_save", _boom)

    result = CliRunner().invoke(
        cli_main,
        ["notebook", "cancel-save-image", "demo-notebook", "--workspace", "Test Workspace"],
    )

    assert result.exit_code != 0
    assert "Could not cancel the image save." in result.output
    assert "notebook-abc" not in result.output


def test_cancel_save_forwards_pick_to_notebook_name_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    seen: dict[str, object] = {}

    def resolve_notebook(*_args, **kwargs):  # noqa: ANN001
        seen["pick"] = kwargs.get("pick")
        seen["require_live"] = kwargs.get("require_live")
        return ("notebook-abc", None)

    monkeypatch.setattr(notebook_lookup_module, "_resolve_notebook_id", resolve_notebook)
    monkeypatch.setattr(
        browser_api_module,
        "cancel_notebook_image_save",
        lambda notebook_id, session=None: True,
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "notebook",
            "cancel-save-image",
            "demo-notebook",
            "--workspace",
            "Test Workspace",
            "--pick",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert seen == {"pick": 2, "require_live": True}


def test_cancel_save_rejects_a_handle_at_the_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config_and_session(monkeypatch, tmp_path)
    monkeypatch.setattr(
        browser_api_module,
        "cancel_notebook_image_save",
        lambda notebook_id, session=None: True,
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "notebook",
            "cancel-save-image",
            "3e1429b2-f0fb-4e39-bb3b-c60bd885df63",
            "--workspace",
            "Test Workspace",
        ],
    )

    assert result.exit_code != 0


def test_cancel_save_is_listed_and_name_only() -> None:
    group_help = CliRunner().invoke(cli_main, ["notebook", "--help"])
    assert "cancel-save-image" in group_help.output
    # The command moved off the image group; no alias is left behind.
    moved_away = CliRunner().invoke(cli_main, ["image", "cancel-save", "--help"])
    assert moved_away.exit_code != 0
    assert "No such command" in moved_away.output

    result = CliRunner().invoke(cli_main, ["notebook", "cancel-save-image", "--help"])
    assert result.exit_code == 0, result.output
    assert "--workspace NAME" in result.output
    assert "--workspace TEXT" not in result.output
    assert "NAME" in result.output.splitlines()[0]


# ---------------------------------------------------------------------------
# notebook create name guard
# ---------------------------------------------------------------------------


def test_name_guard_stops_a_duplicate_before_creating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_api_module,
        "notebook_name_exists",
        lambda name, workspace_id=None, session=None: True,
    )

    with pytest.raises(SystemExit) as excinfo:
        flow_module._reject_taken_notebook_name(
            Context(),
            name="demo",
            workspace_id="ws-1",
            session=FakeWebSession(),
        )
    assert excinfo.value.code == EXIT_VALIDATION_ERROR


def test_name_guard_passes_a_free_name(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_exists(name, workspace_id=None, session=None):  # noqa: ANN001
        seen["name"] = name
        seen["workspace_id"] = workspace_id
        return False

    monkeypatch.setattr(browser_api_module, "notebook_name_exists", fake_exists)

    flow_module._reject_taken_notebook_name(
        Context(),
        name="demo",
        workspace_id="ws-1",
        session=FakeWebSession(),
    )
    assert seen == {"name": "demo", "workspace_id": "ws-1"}


def test_name_guard_steps_aside_when_the_check_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A check that never reached the platform is not evidence of a collision."""

    def _boom(name, workspace_id=None, session=None):  # noqa: ANN001
        raise ValueError("API error: ServiceUnavailable: ...")

    monkeypatch.setattr(browser_api_module, "notebook_name_exists", _boom)

    flow_module._reject_taken_notebook_name(
        Context(),
        name="demo",
        workspace_id="ws-1",
        session=FakeWebSession(),
    )


def test_run_notebook_create_does_not_submit_a_taken_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = ResolvedQuota(
        quota_id="quota-cpu",
        logic_compute_group_id="lcg-test",
        compute_group_name="CPU Pool",
        gpu_count=0,
        cpu_count=4,
        memory_gib=32,
        gpu_type="",
        raw_price={},
    )
    selected = SimpleNamespace(
        project_id="project-1", name="Project One", priority_name="6"
    )
    image = SimpleNamespace(image_id="img-1", url="docker://image", name="Image One")
    submitted: dict[str, Any] = {}

    monkeypatch.setattr(flow_module, "resolve_json_output", lambda _ctx, _json: False)
    monkeypatch.setattr(
        flow_module, "require_web_session", lambda _ctx, hint=None: FakeWebSession()
    )
    monkeypatch.setattr(
        flow_module,
        "load_config",
        lambda _ctx: SimpleNamespace(
            project_order=None,
            notebook_post_start=None,
            shm_size=32,
            projects={},
            profiles={},
        ),
    )
    monkeypatch.setattr(
        flow_module, "_resolve_workspace_id", lambda _ctx, **_kwargs: "ws-1111"
    )
    monkeypatch.setattr(
        flow_module, "resolve_quota", lambda **_kwargs: resolved
    )
    monkeypatch.setattr(
        flow_module, "_fetch_workspace_projects", lambda *_a, **_k: [selected]
    )
    monkeypatch.setattr(
        flow_module, "resolve_notebook_project", lambda *_a, **_k: selected
    )
    monkeypatch.setattr(flow_module, "_fetch_notebook_images", lambda *_a, **_k: [image])
    monkeypatch.setattr(flow_module, "resolve_notebook_image", lambda *_a, **_k: image)

    def _fail_create(*_a, **_k):
        submitted["called"] = True
        return "nb-1"

    monkeypatch.setattr(flow_module, "create_notebook_and_report", _fail_create)
    monkeypatch.setattr(
        browser_api_module,
        "notebook_name_exists",
        lambda name, workspace_id=None, session=None: True,
    )

    with pytest.raises(SystemExit):
        flow_module.run_notebook_create(
            Context(),
            name="demo",
            workspace="cpu",
            workspace_id=None,
            quota="0,4,32",
            project="Project One",
            image="Image One",
            shm_size=None,
            auto_stop=False,
            wait=False,
            post_start=None,
            post_start_script=None,
            json_output=False,
            group="CPU Pool",
        )

    assert "called" not in submitted
