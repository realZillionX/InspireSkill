"""Public-output tests for `inspire account permissions`.

Moved from the deleted `inspire user` group; the command kept its options,
Name-only output and bounding behaviour.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from inspire.cli.context import EXIT_VALIDATION_ERROR
from inspire.cli.main import main as cli_main
from inspire.platform.web import browser_api as browser_api_module

from inspire.cli.commands.account import permissions_cmd as permissions_module

_WORKSPACE_ID = "ws-00000000-0000-0000-0000-0000000000aa"
_SECOND_WORKSPACE_ID = "ws-00000000-0000-0000-0000-0000000000bb"


class _FakeSession:
    workspace_id = _WORKSPACE_ID
    all_workspace_ids = [_WORKSPACE_ID]
    all_workspace_names = {_WORKSPACE_ID: "Default WS"}


def _patch_session(monkeypatch) -> _FakeSession:  # noqa: ANN001
    session = _FakeSession()
    monkeypatch.setattr(permissions_module, "get_web_session", lambda: session)
    return session


def test_account_permissions_json_returns_name_only_permissions(monkeypatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        permissions_module.Config,
        "from_files_and_env",
        classmethod(
            lambda cls, **_: (
                permissions_module.Config(username="user", password="pass"),
                {},
            )
        ),
    )
    captured: dict[str, str] = {}

    def _permissions(*, workspace_id, session=None):  # noqa: ANN001,ARG001
        captured["workspace_id"] = workspace_id
        return ["job.create", "job.read", "job.create"]

    monkeypatch.setattr(browser_api_module, "get_user_permissions", _permissions)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "account", "permissions", "--workspace", "Default WS"],
    )

    assert result.exit_code == 0, result.output
    assert captured["workspace_id"] == _WORKSPACE_ID
    assert json.loads(result.output)["data"] == {
        "items": ["job.create", "job.read"],
    }


def test_account_permissions_workspace_metavar_accepts_name_or_all() -> None:
    result = CliRunner().invoke(cli_main, ["account", "permissions", "--help"])

    assert result.exit_code == 0, result.output
    assert "--workspace NAME|all" in result.output
    assert "--workspace TEXT" not in result.output


def test_account_permissions_default_json_is_bounded(monkeypatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        permissions_module.Config,
        "from_files_and_env",
        classmethod(
            lambda cls, **_: (
                permissions_module.Config(username="user", password="pass"),
                {},
            )
        ),
    )
    monkeypatch.setattr(
        browser_api_module,
        "get_user_permissions",
        lambda **_: [f"permission.{index:02d}" for index in range(25)],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "account",
            "permissions",
            "--workspace",
            "Default WS",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data == {
        "items": [f"permission.{index:02d}" for index in range(20)],
        "shown": 20,
        "total": 25,
        "truncated": True,
    }


def test_account_permissions_all_is_unbounded_without_metadata(monkeypatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        permissions_module.Config,
        "from_files_and_env",
        classmethod(
            lambda cls, **_: (
                permissions_module.Config(username="user", password="pass"),
                {},
            )
        ),
    )
    monkeypatch.setattr(
        browser_api_module,
        "get_user_permissions",
        lambda **_: [f"permission.{index:02d}" for index in range(25)],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "account",
            "permissions",
            "--workspace",
            "Default WS",
            "--all",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert len(data["items"]) == 25
    assert set(data) == {"items"}


def test_account_permissions_workspace_all_fans_out_with_workspace_names(
    monkeypatch,
) -> None:  # noqa: ANN001
    class _AllWorkspaceSession:
        workspace_id = _WORKSPACE_ID
        all_workspace_ids = [_WORKSPACE_ID, _SECOND_WORKSPACE_ID]
        all_workspace_names = {
            _WORKSPACE_ID: "Default WS",
            _SECOND_WORKSPACE_ID: "Research WS",
        }

    monkeypatch.setattr(
        permissions_module,
        "get_web_session",
        lambda: _AllWorkspaceSession(),
    )
    monkeypatch.setattr(
        permissions_module.Config,
        "from_files_and_env",
        classmethod(
            lambda cls, **_: (
                permissions_module.Config(username="user", password="pass"),
                {},
            )
        ),
    )
    calls: list[str] = []

    def fake_permissions(*, workspace_id, session=None):  # noqa: ANN001,ARG001
        calls.append(workspace_id)
        if workspace_id == _WORKSPACE_ID:
            return ["job.read", "job.create", "job.read"]
        return ["serving.read"]

    monkeypatch.setattr(
        browser_api_module,
        "get_user_permissions",
        fake_permissions,
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "account", "permissions", "--workspace", "all"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [_WORKSPACE_ID, _SECOND_WORKSPACE_ID]
    assert json.loads(result.output)["data"] == {
        "items": [
            {"workspace": "Default WS", "permission": "job.create"},
            {"workspace": "Default WS", "permission": "job.read"},
            {"workspace": "Research WS", "permission": "serving.read"},
        ]
    }
    assert _WORKSPACE_ID not in result.output
    assert _SECOND_WORKSPACE_ID not in result.output

    human = CliRunner().invoke(
        cli_main,
        ["account", "permissions", "--workspace", "all"],
    )
    assert human.exit_code == 0, human.output
    assert "Default WS: job.create" in human.output
    assert "Research WS: serving.read" in human.output
    assert _WORKSPACE_ID not in human.output
    assert _SECOND_WORKSPACE_ID not in human.output


@pytest.mark.parametrize(
    "args",
    (["account", "permissions", "--workspace", "Default WS"],),
)
def test_account_permissions_rejects_limit_with_all(args) -> None:
    result = CliRunner().invoke(
        cli_main,
        ["--json", *args, "--limit", "2", "--all"],
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "ValidationError"


def test_account_permissions_rejects_workspace_id(monkeypatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        permissions_module.Config,
        "from_files_and_env",
        classmethod(
            lambda cls, **_: (
                permissions_module.Config(username="user", password="pass"),
                {},
            )
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["account", "permissions", "--workspace", _WORKSPACE_ID],
    )

    assert result.exit_code != 0
    assert "workspace name" in result.output
    assert _WORKSPACE_ID not in result.output
