"""Focused tests for name-only project selection in job creation."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from inspire.cli.commands.job import job_create
from inspire.cli.context import EXIT_VALIDATION_ERROR
from inspire.cli.main import main as cli_main
from inspire.cli.utils import job_submit
from inspire.config import Config, ConfigError
from inspire.platform.web import browser_api

WORKSPACE_ID = "ws-11111111-1111-1111-1111-111111111111"
PROJECT_ID = "project-11111111-1111-1111-1111-111111111111"
OTHER_PROJECT_ID = "project-22222222-2222-2222-2222-222222222222"


def _project(project_id: str, name: str) -> browser_api.ProjectInfo:
    return browser_api.ProjectInfo(
        project_id=project_id,
        name=name,
        workspace_id=WORKSPACE_ID,
    )


def test_job_create_rejects_project_id_before_platform_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        job_create.Config,
        "from_files_and_env",
        lambda: (Config(username="", password=""), {}),
    )

    def _unexpected_session() -> object:
        raise AssertionError("ID-shaped project input was not rejected before lookup")

    monkeypatch.setattr(job_create, "get_web_session", _unexpected_session)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "create",
            "--name",
            "train",
            "--quota",
            "0,4,16",
            "--command",
            "python train.py",
            "--workspace",
            "Workspace",
            "--project",
            PROJECT_ID,
            "--group",
            "CPU",
            "--image",
            "python:3.12",
        ],
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR
    payload = json.loads(result.output)
    assert payload["error"]["type"] == "ValidationError"
    assert "project name" in payload["error"]["message"]
    assert PROJECT_ID not in result.output


def test_job_project_selector_rejects_project_id_before_session_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_session() -> object:
        raise AssertionError("ID-shaped project input was not rejected before lookup")

    monkeypatch.setattr(job_submit.web_session_module, "get_web_session", _unexpected_session)

    with pytest.raises(ConfigError, match="project name"):
        job_submit.select_project_for_workspace(
            Config(username="", password=""),
            workspace_id=WORKSPACE_ID,
            requested=PROJECT_ID,
        )


def test_job_project_selector_uses_live_name_from_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = object()
    expected = _project(PROJECT_ID, "Project One")
    other = _project(OTHER_PROJECT_ID, "Other Project")
    config = Config(
        username="user",
        password="pass",
        projects={"Project One": "Project One"},
    )

    monkeypatch.setattr(job_submit.web_session_module, "get_web_session", lambda: session)
    monkeypatch.setattr(
        job_submit.browser_api_module,
        "list_projects",
        lambda **_kwargs: [expected, other],
    )
    monkeypatch.setattr(
        job_submit.browser_api_module,
        "check_scheduling_health",
        lambda **_kwargs: set(),
    )

    selected, message = job_submit.select_project_for_workspace(
        config,
        workspace_id=WORKSPACE_ID,
        requested="project one",
    )

    assert selected is expected
    assert message is None
