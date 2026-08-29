"""Tests for ``notebook.EstimateSaveMirrorSize`` and ``notebook save-image --dry-run``.

The estimate is the only thing that tells a caller how long ``notebook
save-image`` will lock the notebook for, so the boundary that matters here is
that a *failed* estimate never reads as "there is nothing to snapshot" —
neither zero bytes nor a stopped notebook.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.notebook import notebook_lookup as notebook_lookup_module
from inspire.cli.context import EXIT_API_ERROR, EXIT_VALIDATION_ERROR
from inspire.cli.main import main as cli_main
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web import session as web_session_module
from inspire.platform.web.browser_api import notebooks as notebooks_module
from inspire.platform.web.browser_api.notebooks import (
    NotebookImageSizeEstimate,
    estimate_notebook_image_size,
)
from inspire.platform.web.session import TransientAPIError


class _FakeSession:
    workspace_id = "ws-test-workspace"
    all_workspace_ids = ["ws-test-workspace"]
    all_workspace_names = {"ws-test-workspace": "Test Workspace"}
    storage_state: dict = {}


def _json_data(output: str) -> Any:
    parsed = json.loads(output)
    return parsed.get("data", parsed)


def _patch_estimate_call(monkeypatch: pytest.MonkeyPatch, responder) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_notebook_v2(session, action, body=None, *, timeout=30):  # noqa: ANN001
        captured["action"] = action
        captured["body"] = body
        return responder()

    monkeypatch.setattr(notebooks_module, "_notebook_v2", fake_notebook_v2)
    monkeypatch.setattr(notebooks_module, "get_web_session", lambda: _FakeSession())
    return captured


# ---------------------------------------------------------------------------
# Wrapper
# ---------------------------------------------------------------------------


def test_estimate_reads_the_string_wire_value_as_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # discovery declares int64, the gateway answers a decimal string.
    captured = _patch_estimate_call(
        monkeypatch, lambda: {"active_snapshot_size": "59084800"}
    )

    estimate = estimate_notebook_image_size(notebook_id="nb-1")

    assert captured["action"] == "EstimateSaveMirrorSize"
    assert captured["body"] == {"notebook_id": "nb-1"}
    assert estimate == NotebookImageSizeEstimate(
        size_bytes=59_084_800, notebook_running=True
    )


def test_estimate_accepts_a_real_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_estimate_call(monkeypatch, lambda: {"active_snapshot_size": "0"})

    estimate = estimate_notebook_image_size(notebook_id="nb-1")

    assert estimate.size_bytes == 0
    assert estimate.notebook_running is True


def test_estimate_reports_a_stopped_notebook_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise():
        raise ValueError(
            "API error: InvalidParameter: Cannot save image of "
            "non-running notebook: 59402e86-295a-4578-90a7-7f7b0b930c82"
        )

    _patch_estimate_call(monkeypatch, _raise)

    estimate = estimate_notebook_image_size(notebook_id="nb-1")

    assert estimate == NotebookImageSizeEstimate(size_bytes=None, notebook_running=False)


def test_estimate_propagates_a_throttled_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # TransientAPIError subclasses ValueError; folding it into "not running"
    # would turn a rate limit into a claim about the notebook.
    def _raise():
        raise TransientAPIError("API error: Throttling: slow down")

    _patch_estimate_call(monkeypatch, _raise)

    with pytest.raises(TransientAPIError):
        estimate_notebook_image_size(notebook_id="nb-1")


def test_estimate_propagates_an_unknown_notebook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise():
        raise ValueError("API error: ResourceNotFound: notebook not found")

    _patch_estimate_call(monkeypatch, _raise)

    with pytest.raises(ValueError, match="ResourceNotFound"):
        estimate_notebook_image_size(notebook_id="nb-1")


def test_estimate_rejects_a_missing_size_field(monkeypatch: pytest.MonkeyPatch) -> None:
    # The gateway emits unpopulated fields, so an absent key is an unknown
    # shape, not a zero-byte snapshot.
    _patch_estimate_call(monkeypatch, dict)

    with pytest.raises(ValueError, match="active_snapshot_size"):
        estimate_notebook_image_size(notebook_id="nb-1")


def test_estimate_requires_a_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_notebook_v2(session, action, body=None, *, timeout=30):  # noqa: ANN001
        calls.append(action)
        return {}

    monkeypatch.setattr(notebooks_module, "_notebook_v2", fake_notebook_v2)

    with pytest.raises(ValueError, match="notebook handle"):
        estimate_notebook_image_size(notebook_id="  ")
    assert calls == []


# ---------------------------------------------------------------------------
# `notebook save-image`
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> config_module.Config:
    return config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
    )


def _patch_save_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    estimate: Any = NotebookImageSizeEstimate(size_bytes=59_084_800, notebook_running=True),
    estimate_error: Optional[Exception] = None,
) -> list[dict[str, Any]]:
    """Wire `notebook save-image` to fakes and return the list of save calls it made."""
    config = _make_config(tmp_path)

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(web_session_module, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(
        notebook_lookup_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: ("notebook-abc", None),
    )

    def fake_estimate(*, notebook_id, session=None):  # noqa: ANN001
        if estimate_error is not None:
            raise estimate_error
        return estimate

    monkeypatch.setattr(
        browser_api_module, "estimate_notebook_image_size", fake_estimate
    )

    saves: list[dict[str, Any]] = []

    def fake_save(notebook_id, name, version="v1", description="", flatten=False, session=None):  # noqa: ANN001
        saves.append({"notebook_id": notebook_id, "name": name, "version": version})
        return {"image": {"image_id": "img-saved-001"}}

    monkeypatch.setattr(browser_api_module, "save_notebook_as_image", fake_save)
    monkeypatch.setattr(
        browser_api_module, "list_images_by_source", lambda source, session=None: []
    )
    return saves


def _save_argv(*extra: str) -> list[str]:
    return [
        "notebook",
        "save-image",
        "demo-notebook",
        "--workspace",
        "Test Workspace",
        "-n",
        "saved-img",
        *extra,
    ]


def test_dry_run_prints_the_estimate_and_saves_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    saves = _patch_save_command(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli_main, _save_argv("--dry-run"))

    assert result.exit_code == 0
    assert "56.35 MiB" in result.output
    assert "saved-img:v1" in result.output
    assert saves == []


def test_dry_run_json_reports_bytes_and_a_readable_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    saves = _patch_save_command(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli_main, ["--json", *_save_argv("--dry-run")])

    assert result.exit_code == 0
    assert _json_data(result.output) == {
        "dry_run": True,
        "notebook": "demo-notebook",
        "name": "saved-img:v1",
        "flatten": False,
        "estimated_size_bytes": 59_084_800,
        "estimated_size": "56.35 MiB",
    }
    assert saves == []


def test_dry_run_fails_when_the_platform_will_not_estimate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    saves = _patch_save_command(
        monkeypatch,
        tmp_path,
        estimate_error=TransientAPIError("API error: Throttling: slow down"),
    )

    result = CliRunner().invoke(cli_main, _save_argv("--dry-run"))

    # An unavailable estimate is not a zero-byte estimate.
    assert result.exit_code == EXIT_API_ERROR
    assert "0 B" not in result.output
    assert saves == []


def test_stopped_notebook_is_refused_before_any_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    saves = _patch_save_command(
        monkeypatch,
        tmp_path,
        estimate=NotebookImageSizeEstimate(size_bytes=None, notebook_running=False),
    )

    result = CliRunner().invoke(cli_main, _save_argv())

    assert result.exit_code == EXIT_VALIDATION_ERROR
    assert "not running" in result.output
    assert "inspire notebook start demo-notebook" in result.output
    assert saves == []


def test_save_announces_the_estimate_before_it_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    saves = _patch_save_command(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli_main, _save_argv())

    assert result.exit_code == 0
    assert "Estimated snapshot: 56.35 MiB" in result.output
    assert "cannot be used until the save finishes" in result.output
    assert len(saves) == 1


def test_save_json_carries_the_estimate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_save_command(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli_main, ["--json", *_save_argv()])

    assert result.exit_code == 0
    payload = _json_data(result.output)
    assert payload["name"] == "saved-img:v1"
    assert payload["status"] == "saving"
    assert payload["estimated_size_bytes"] == 59_084_800
    assert payload["estimated_size"] == "56.35 MiB"
    assert "img-saved-001" not in result.output


def test_an_unavailable_estimate_does_not_block_the_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    saves = _patch_save_command(
        monkeypatch,
        tmp_path,
        estimate_error=TransientAPIError("API error: Throttling: slow down"),
    )

    result = CliRunner().invoke(cli_main, ["--json", *_save_argv()])

    assert result.exit_code == 0
    payload = _json_data(result.output)
    assert "estimated_size_bytes" not in payload
    assert len(saves) == 1
