"""CLI tests for `inspire user ssh-keys`."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from click.testing import CliRunner

from inspire.cli.commands.user import user_commands as user_cmd_module
from inspire.cli.context import EXIT_VALIDATION_ERROR
from inspire.cli.main import main as cli_main
from inspire.cli.utils.resource_index import (
    ResourceIdentity,
    ResourceIndex,
    ResourceScope,
)
from inspire.platform.web import browser_api as browser_api_module

_WORKSPACE_ID = "ws-00000000-0000-0000-0000-0000000000aa"
_SECOND_WORKSPACE_ID = "ws-00000000-0000-0000-0000-0000000000bb"


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


def _enable_ssh_key_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> tuple[_FakeSession, ResourceIndex, ResourceScope]:  # noqa: ANN001
    session = _patch_session(monkeypatch)
    session.base_url = "https://inspire.example"
    session.user_detail = {"id": "user-one"}
    session.login_username = "alice"
    index = ResourceIndex(tmp_path / "resource-index.sqlite3")
    scope = ResourceScope(
        base_url="https://inspire.example",
        subject_id="user-one",
        resource_type="ssh-key",
        owner_scope="self",
    )
    monkeypatch.setattr(
        ResourceIndex,
        "for_account",
        classmethod(lambda cls, account=None: index),
    )
    return session, index, scope


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


def _key_items(count: int) -> list[dict[str, str]]:
    return [
        {
            "id": f"internal-key-{index}",
            "name": f"key-{index:02d}",
            "fingerprint": f"SHA256:{index}",
            "created_at": f"2026-08-{index + 1:02d}",
            "status": "active",
        }
        for index in range(count)
    ]


def test_api_keys_default_json_is_bounded_and_name_only(monkeypatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "list_user_api_keys",
        lambda **_: _key_items(25),
    )

    result = CliRunner().invoke(cli_main, ["--json", "user", "api-keys"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data == {
        "items": [{"name": f"key-{index:02d}"} for index in range(20)],
        "shown": 20,
        "total": 25,
        "truncated": True,
    }
    assert "fingerprint" not in result.output
    assert "created_at" not in result.output
    assert "status" not in result.output


def test_api_keys_limit_human_is_compact_and_all_is_unbounded(monkeypatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "list_user_api_keys",
        lambda **_: _key_items(25),
    )
    runner = CliRunner()

    limited = runner.invoke(cli_main, ["user", "api-keys", "--limit", "2"])
    assert limited.exit_code == 0, limited.output
    assert limited.output.splitlines() == [
        "key-00",
        "key-01",
        "Showing 2 of 25. Use --all for the full list.",
    ]

    unbounded = runner.invoke(cli_main, ["--json", "user", "api-keys", "--all"])
    assert unbounded.exit_code == 0, unbounded.output
    data = json.loads(unbounded.output)["data"]
    assert len(data["items"]) == 25
    assert set(data) == {"items"}


def test_ssh_keys_default_json_is_bounded_and_name_only(monkeypatch) -> None:
    _patch_session(monkeypatch)
    items = _key_items(25)
    calls: list[tuple[int, int]] = []

    def fake_list(*, page=1, page_size=100, session=None):  # noqa: ANN001,ARG001
        calls.append((page, page_size))
        start = (page - 1) * page_size
        return items[start : start + page_size], len(items)

    monkeypatch.setattr(browser_api_module, "list_user_ssh_keys", fake_list)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "user", "ssh-keys", "list"],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert calls == [(1, 20)]
    assert data == {
        "items": [{"name": f"key-{index:02d}"} for index in range(20)],
        "shown": 20,
        "total": 25,
        "truncated": True,
    }
    assert "fingerprint" not in result.output
    assert "created_at" not in result.output


def test_ssh_keys_all_paginates_without_truncation_metadata(monkeypatch) -> None:
    _patch_session(monkeypatch)
    items = _key_items(5)
    calls: list[int] = []

    def fake_list(*, page=1, page_size=100, session=None):  # noqa: ANN001,ARG001
        calls.append(page)
        start = (page - 1) * 2
        return items[start : start + 2], len(items)

    monkeypatch.setattr(browser_api_module, "list_user_ssh_keys", fake_list)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "user", "ssh-keys", "list", "--all"],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert calls == [1, 2, 3]
    assert data == {"items": [{"name": f"key-{index:02d}"} for index in range(5)]}


@pytest.mark.parametrize(
    "args",
    (
        ["user", "api-keys"],
        ["user", "ssh-keys", "list"],
    ),
)
def test_user_key_lists_reject_limit_with_all_as_single_json_document(args) -> None:
    result = CliRunner().invoke(
        cli_main,
        ["--json", *args, "--limit", "2", "--all"],
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "ValidationError"
    assert "either --limit or --all" in payload["error"]["message"]


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
    assert result.output == "OK SSH key created: main-key\n"
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


def test_ssh_keys_add_writes_through_to_cache(monkeypatch, tmp_path) -> None:
    _session, index, scope = _enable_ssh_key_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(browser_api_module, "list_user_ssh_keys", lambda **_: ([], 0))
    monkeypatch.setattr(
        browser_api_module,
        "create_user_ssh_key",
        lambda **_: {"ssh_id": "ssh-created"},
    )

    result = CliRunner().invoke(
        cli_main,
        ["user", "ssh-keys", "add", "main-key", "--public-key", _valid_public_key()],
    )

    assert result.exit_code == 0
    assert [item.resource_id for item in index.lookup(scope, "main-key")] == [
        "ssh-created"
    ]
    assert "ssh-created" not in result.output


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
    assert result.output == "OK SSH key deleted: main-key\n"
    assert "ssh-12345678" not in result.output


def test_ssh_keys_delete_forwards_pick(monkeypatch) -> None:
    _patch_session(monkeypatch)
    seen: list[int | None] = []
    monkeypatch.setattr(
        user_cmd_module,
        "_resolve_ssh_key_by_name",
        lambda _ctx, _name, *, session, pick=None, require_live=False: (
            seen.append(pick)
            or {"name": "main-key", "ssh_id": "ssh-internal"}
        ),
    )
    monkeypatch.setattr(
        browser_api_module,
        "delete_user_ssh_key",
        lambda *_args, **_kwargs: {},
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "user",
            "ssh-keys",
            "delete",
            "main-key",
            "--pick",
            "2",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == [2]
    assert "ssh-internal" not in result.output


def test_ssh_keys_delete_tombstones_cached_identity(monkeypatch, tmp_path) -> None:
    _session, index, scope = _enable_ssh_key_cache(monkeypatch, tmp_path)
    index.upsert(
        scope,
        [ResourceIdentity(resource_id="ssh-cached", name="main-key")],
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_user_ssh_keys",
        lambda **_: pytest.fail("fresh cache should avoid the initial list"),
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        browser_api_module,
        "delete_user_ssh_key",
        lambda ssh_id, **_: deleted.append(ssh_id) or {},
    )

    result = CliRunner().invoke(
        cli_main,
        ["user", "ssh-keys", "delete", "main-key", "--yes"],
    )

    assert result.exit_code == 0
    assert deleted == ["ssh-cached"]
    assert "ssh-cached" not in result.output
    assert index.lookup(scope, "main-key", fresh_only=False) == []
    cached = index.lookup_id(scope, "ssh-cached", include_tombstoned=True)
    assert cached is not None
    assert cached.tombstoned_at is not None


def test_ssh_keys_delete_stale_cache_re_resolves_once_and_deletes_new_handle(
    monkeypatch, tmp_path
) -> None:
    _session, index, scope = _enable_ssh_key_cache(monkeypatch, tmp_path)
    index.upsert(
        scope,
        [ResourceIdentity(resource_id="ssh-old", name="main-key")],
    )
    list_calls = 0

    def fake_list(**_):
        nonlocal list_calls
        list_calls += 1
        return ([{"id": "ssh-new", "name": "main-key"}], 1)

    delete_calls: list[str] = []

    def fake_delete(ssh_id, **_):
        delete_calls.append(ssh_id)
        if ssh_id == "ssh-old":
            raise RuntimeError("API returned 404: SSH key not found")
        return {}

    monkeypatch.setattr(browser_api_module, "list_user_ssh_keys", fake_list)
    monkeypatch.setattr(browser_api_module, "delete_user_ssh_key", fake_delete)

    result = CliRunner().invoke(
        cli_main,
        ["user", "ssh-keys", "delete", "main-key", "--yes"],
    )

    assert result.exit_code == 0
    assert list_calls == 1
    assert delete_calls == ["ssh-old", "ssh-new"]
    assert "ssh-old" not in result.output
    assert "ssh-new" not in result.output
    assert index.lookup(scope, "main-key", fresh_only=False) == []
    old = index.lookup_id(scope, "ssh-old", include_tombstoned=True)
    new = index.lookup_id(scope, "ssh-new", include_tombstoned=True)
    assert old is not None and old.tombstoned_at is not None
    assert new is not None and new.tombstoned_at is not None


def test_ssh_keys_delete_network_error_does_not_retry(monkeypatch, tmp_path) -> None:
    _session, index, scope = _enable_ssh_key_cache(monkeypatch, tmp_path)
    index.upsert(
        scope,
        [ResourceIdentity(resource_id="ssh-cached", name="main-key")],
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_user_ssh_keys",
        lambda **_: pytest.fail("network failure must not trigger a live retry"),
    )
    delete_calls: list[str] = []

    def fake_delete(ssh_id, **_):
        delete_calls.append(ssh_id)
        raise RuntimeError("network timeout")

    monkeypatch.setattr(browser_api_module, "delete_user_ssh_key", fake_delete)

    result = CliRunner().invoke(
        cli_main,
        ["user", "ssh-keys", "delete", "main-key", "--yes"],
    )

    assert result.exit_code != 0
    assert delete_calls == ["ssh-cached"]
    assert "ssh-cached" not in result.output
    assert [item.resource_id for item in index.lookup(scope, "main-key")] == [
        "ssh-cached"
    ]


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
    assert "SSH key name" in result.output


def test_whoami_json_projects_public_identity_fields(monkeypatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "get_current_user",
        lambda **_: {
            "id": "user-12345678-1234-1234-1234-123456789abc",
            "display_name": "Ada",
            "username": "253108120116",
            "email": "ada@example.com",
            "global_role": "member",
            "extra_info": {
                "login_name": "usr_391",
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
        "role": "member",
        "email": "ada@example.com",
    }

    human = CliRunner().invoke(cli_main, ["user", "whoami"])
    assert human.exit_code == 0
    assert human.output.splitlines() == [
        "Name: Ada",
        "Role: member",
        "Email: ada@example.com",
    ]


@pytest.mark.parametrize(
    "login_value",
    ("user-hidden", "usr_391", "student-42", "253108120116"),
)
def test_whoami_never_uses_login_identifiers_as_name(
    monkeypatch,
    login_value: str,
) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "get_current_user",
        lambda **_: {
            "username": login_value,
            "login_name": login_value,
            "global_role": "member",
        },
    )

    result = CliRunner().invoke(cli_main, ["--json", "user", "whoami"])

    assert result.exit_code == 0
    assert json.loads(result.output)["data"] == {"role": "member"}
    assert login_value not in result.output

    human = CliRunner().invoke(cli_main, ["user", "whoami"])
    assert human.exit_code == 0
    assert human.output.splitlines() == ["Role: member"]
    assert login_value not in human.output


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


def test_user_quota_default_json_bounds_flat_rows_without_changing_shape(
    monkeypatch,
) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "get_user_quota",
        lambda **_: {
            f"quota-{index:02d}": index
            for index in range(25)
        },
    )

    result = CliRunner().invoke(cli_main, ["--json", "user", "quota"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["quota"] == {
        f"quota-{index:02d}": index
        for index in range(20)
    }
    assert data["shown"] == 20
    assert data["total"] == 25
    assert data["truncated"] is True


def test_user_quota_limit_human_and_all_json(monkeypatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "get_user_quota",
        lambda **_: {
            f"quota-{index:02d}": index
            for index in range(25)
        },
    )
    runner = CliRunner()

    limited = runner.invoke(cli_main, ["user", "quota", "--limit", "2"])
    assert limited.exit_code == 0, limited.output
    assert limited.output.splitlines() == [
        "quota-00: 0",
        "quota-01: 1",
        "Showing 2 of 25. Use --all for the full list.",
    ]

    unbounded = runner.invoke(cli_main, ["--json", "user", "quota", "--all"])
    assert unbounded.exit_code == 0, unbounded.output
    data = json.loads(unbounded.output)["data"]
    assert len(data["quota"]) == 25
    assert set(data) == {"quota"}


def test_user_quota_bounds_long_scalar_lists(monkeypatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "get_user_quota",
        lambda **_: {
            "available_profiles": [
                f"profile-{index:02d}"
                for index in range(25)
            ]
        },
    )

    result = CliRunner().invoke(cli_main, ["--json", "user", "quota"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["quota"]["available_profiles"] == [
        f"profile-{index:02d}"
        for index in range(20)
    ]
    assert data["shown"] == 20
    assert data["total"] == 25
    assert data["truncated"] is True


def test_user_permissions_json_returns_name_only_permissions(monkeypatch) -> None:
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
        "items": ["job.create", "job.read"],
    }


def test_user_permissions_workspace_metavar_accepts_name_or_all() -> None:
    result = CliRunner().invoke(cli_main, ["user", "permissions", "--help"])

    assert result.exit_code == 0, result.output
    assert "--workspace NAME|all" in result.output
    assert "--workspace TEXT" not in result.output


def test_user_permissions_default_json_is_bounded(monkeypatch) -> None:
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
    monkeypatch.setattr(
        browser_api_module,
        "get_user_permissions",
        lambda **_: [f"permission.{index:02d}" for index in range(25)],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "user",
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


def test_user_permissions_all_is_unbounded_without_metadata(monkeypatch) -> None:
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
    monkeypatch.setattr(
        browser_api_module,
        "get_user_permissions",
        lambda **_: [f"permission.{index:02d}" for index in range(25)],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "user",
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


def test_user_permissions_workspace_all_fans_out_with_workspace_names(
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
        user_cmd_module,
        "get_web_session",
        lambda: _AllWorkspaceSession(),
    )
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
        ["--json", "user", "permissions", "--workspace", "all"],
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
        ["user", "permissions", "--workspace", "all"],
    )
    assert human.exit_code == 0, human.output
    assert "Default WS: job.create" in human.output
    assert "Research WS: serving.read" in human.output
    assert _WORKSPACE_ID not in human.output
    assert _SECOND_WORKSPACE_ID not in human.output


@pytest.mark.parametrize(
    "args",
    (
        ["user", "quota"],
        ["user", "permissions", "--workspace", "Default WS"],
    ),
)
def test_user_quota_and_permissions_reject_limit_with_all(args) -> None:
    result = CliRunner().invoke(
        cli_main,
        ["--json", *args, "--limit", "2", "--all"],
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "ValidationError"


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
