"""CLI tests for `inspire user ssh-keys`."""

from __future__ import annotations

import base64
import json
from typing import Any

from click.testing import CliRunner

from inspire.cli.commands.user import user_commands as user_cmd_module
from inspire.cli.context import EXIT_VALIDATION_ERROR
from inspire.cli.main import main as cli_main
from inspire.platform.web import browser_api as browser_api_module

_WORKSPACE_ID = "ws-00000000-0000-0000-0000-0000000000aa"


class _FakeSession:
    workspace_id = _WORKSPACE_ID
    all_workspace_ids = [_WORKSPACE_ID]
    all_workspace_names = {_WORKSPACE_ID: "Default WS"}


def _patch_session(monkeypatch) -> _FakeSession:  # noqa: ANN001
    session = _FakeSession()
    monkeypatch.setattr(user_cmd_module, "get_web_session", lambda: session)
    return session


def _valid_public_key() -> str:
    payload = base64.b64encode(b"not-a-real-key-but-valid-base64").decode("ascii")
    return f"ssh-ed25519 {payload} codex@example"


def test_ssh_keys_list_human_hides_raw_ids(monkeypatch) -> None:
    _patch_session(monkeypatch)

    def _fake_list(*, page=1, page_size=100, session=None):  # noqa: ANN001,ARG001
        return (
            [
                {
                    "id": "ssh-12345678-1234-1234-1234-123456789abc",
                    "name": "main-key",
                    "fingerprint": "SHA256:abc",
                }
            ],
            1,
        )

    monkeypatch.setattr(browser_api_module, "list_user_ssh_keys", _fake_list)

    result = CliRunner().invoke(cli_main, ["user", "ssh-keys", "list"])

    assert result.exit_code == 0
    assert "main-key" in result.output
    assert "ssh-12345678" not in result.output


def test_ssh_keys_list_json_omits_raw_ids(monkeypatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "list_user_ssh_keys",
        lambda **_: (
            [{"id": "ssh-12345678-1234-1234-1234-123456789abc", "name": "main-key"}],
            1,
        ),
    )

    result = CliRunner().invoke(cli_main, ["--json", "user", "ssh-keys", "list"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "id" not in payload["data"]["items"][0]
    assert payload["data"]["items"] == [{"name": "main-key"}]


def test_ssh_keys_add_validates_and_uses_content(monkeypatch) -> None:
    _patch_session(monkeypatch)
    calls: dict[str, Any] = {}
    monkeypatch.setattr(browser_api_module, "list_user_ssh_keys", lambda **_: ([], 0))

    def _fake_create(*, name, content, session=None):  # noqa: ANN001,ARG001
        calls["name"] = name
        calls["content"] = content
        return {"ssh_id": "ssh-12345678-1234-1234-1234-123456789abc"}

    monkeypatch.setattr(browser_api_module, "create_user_ssh_key", _fake_create)

    result = CliRunner().invoke(
        cli_main,
        ["user", "ssh-keys", "add", "main-key", "--public-key", _valid_public_key()],
    )

    assert result.exit_code == 0
    assert calls == {"name": "main-key", "content": _valid_public_key()}
    assert "ssh-12345678" not in result.output


def test_ssh_keys_add_json_ignores_platform_result(monkeypatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(browser_api_module, "list_user_ssh_keys", lambda **_: ([], 0))
    monkeypatch.setattr(
        browser_api_module,
        "create_user_ssh_key",
        lambda **_: {
            "ssh_id": "ssh-12345678-1234-1234-1234-123456789abc",
            "result": {"debug": True},
        },
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "user",
            "ssh-keys",
            "add",
            "main-key",
            "--public-key",
            _valid_public_key(),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["data"] == {
        "name": "main-key",
        "status": "created",
    }


def test_ssh_keys_add_rejects_invalid_public_key(monkeypatch) -> None:
    _patch_session(monkeypatch)

    result = CliRunner().invoke(
        cli_main,
        ["user", "ssh-keys", "add", "main-key", "--public-key", "not-a-key"],
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR
    assert "OpenSSH public key format" in result.output


def test_ssh_keys_delete_resolves_by_name(monkeypatch) -> None:
    _patch_session(monkeypatch)
    calls: dict[str, Any] = {}
    monkeypatch.setattr(
        browser_api_module,
        "list_user_ssh_keys",
        lambda **_: (
            [
                {
                    "id": "ssh-12345678-1234-1234-1234-123456789abc",
                    "name": "main-key",
                }
            ],
            1,
        ),
    )

    def _fake_delete(ssh_id, *, session=None):  # noqa: ANN001,ARG001
        calls["ssh_id"] = ssh_id
        return {}

    monkeypatch.setattr(browser_api_module, "delete_user_ssh_key", _fake_delete)

    result = CliRunner().invoke(
        cli_main,
        ["user", "ssh-keys", "delete", "main-key", "--yes"],
    )

    assert result.exit_code == 0
    assert calls["ssh_id"] == "ssh-12345678-1234-1234-1234-123456789abc"
    assert "main-key" in result.output
    assert "ssh-12345678" not in result.output


def test_ssh_keys_delete_rejects_id_shaped_input(monkeypatch) -> None:
    _patch_session(monkeypatch)

    result = CliRunner().invoke(
        cli_main,
        [
            "user",
            "ssh-keys",
            "delete",
            "ssh-12345678-1234-1234-1234-123456789abc",
            "--yes",
        ],
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR
    assert "take a SSH key name" in result.output


def test_whoami_json_projects_public_identity_fields(monkeypatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "get_current_user",
        lambda **_: {
            "id": "user-12345678-1234-1234-1234-123456789abc",
            "name": "Ada",
            "email": "ada@example.com",
            "global_role": "member",
            "extra_info": {
                "login_name": "ada",
                "workspace_id": _WORKSPACE_ID,
                "debug": "drop",
            },
            "raw": {"token": "drop"},
        },
    )

    result = CliRunner().invoke(cli_main, ["--json", "user", "whoami"])

    assert result.exit_code == 0
    assert json.loads(result.output)["data"] == {
        "name": "Ada",
        "login": "ada",
        "role": "member",
        "email": "ada@example.com",
    }


def test_user_quota_json_drops_ids_and_engineering_fields(monkeypatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "get_user_quota",
        lambda **_: {
            "gpu": 8,
            "workspace_id": _WORKSPACE_ID,
            "raw": {"quota_id": "quota-12345678-1234-1234-1234-123456789abc"},
            "limits": {
                "cpu": 80,
                "result": {"debug": True},
            },
        },
    )

    result = CliRunner().invoke(cli_main, ["--json", "user", "quota"])

    assert result.exit_code == 0
    assert json.loads(result.output)["data"] == {
        "quota": {
            "gpu": 8,
            "limits": {"cpu": 80},
        }
    }


def test_user_permissions_json_uses_workspace_name(monkeypatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        user_cmd_module.Config,
        "from_files_and_env",
        classmethod(
            lambda cls, **_: (
                user_cmd_module.Config(username="user", password="pass"),
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
        ["--json", "user", "permissions", "--workspace", "Default WS"],
    )

    assert result.exit_code == 0, result.output
    assert captured["workspace_id"] == _WORKSPACE_ID
    assert json.loads(result.output)["data"] == {
        "workspace": "Default WS",
        "permissions": ["job.create", "job.read"],
    }


def test_user_permissions_rejects_workspace_id(monkeypatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        user_cmd_module.Config,
        "from_files_and_env",
        classmethod(
            lambda cls, **_: (
                user_cmd_module.Config(username="user", password="pass"),
                {},
            )
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["user", "permissions", "--workspace", _WORKSPACE_ID],
    )

    assert result.exit_code != 0
    assert "workspace name" in result.output
    assert _WORKSPACE_ID not in result.output
