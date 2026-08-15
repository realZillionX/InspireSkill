"""`inspire model delete`: the deployment check, the refusals, and the wrapper.

Deleting a model is not version-scoped -- the whole entry goes and every
deployment that still points at any of its versions loses what it was serving
-- so most of what is pinned here is what has to happen *before* the delete
Action is allowed to run at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.model import model_commands as model_commands_module
from inspire.cli.main import main as cli_main
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import models as models_module

_WORKSPACE_ID = "ws-11111111-1111-1111-1111-111111111111"
_MODEL_ID = "model-secret-123"
_WORKSPACE = "训练空间"

# Indices into the serving status enum `model-hub` reports.
_RUNNING = 4
_STOPPED = 7
_FAILED = 3


class _Session:
    workspace_id = _WORKSPACE_ID
    all_workspace_ids = [_WORKSPACE_ID]
    all_workspace_names = {_WORKSPACE_ID: _WORKSPACE}
    storage_state: dict[str, Any] = {}


def _json_data(output: str) -> Any:
    parsed = json.loads(output)
    return parsed.get("data", parsed)


def _versions(*numbers: int) -> dict[str, Any]:
    return {
        "list": [{"model": {"version": number}} for number in numbers],
        "total": len(numbers),
    }


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    versions: dict[str, Any] | None = None,
    servings: dict[int, list[dict[str, Any]]] | None = None,
    pending: bool = False,
) -> list[tuple[str, Any]]:
    """Wire the command onto fakes and record the calls it makes, in order."""
    calls: list[tuple[str, Any]] = []
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
    )
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **kwargs: (config, {})),
    )
    monkeypatch.setattr(model_commands_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        model_commands_module,
        "select_workspace_id",
        lambda **_kwargs: _WORKSPACE_ID,
    )
    monkeypatch.setattr(
        browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-secret-123"},
    )
    monkeypatch.setattr(
        model_commands_module,
        "_resolve_model_name",
        lambda *args, **kwargs: (calls.append(("resolve", args[1])), _MODEL_ID)[1],
    )

    def _fake_versions(**kwargs: Any) -> dict[str, Any]:
        calls.append(("versions", kwargs["model_id"]))
        return versions if versions is not None else _versions(1)

    def _fake_servings(**kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        calls.append(("servings", kwargs["version"]))
        items = (servings or {}).get(int(kwargs["version"]), [])
        return items, len(items)

    def _fake_pending(**kwargs: Any) -> dict[str, Any]:
        calls.append(("pending", kwargs["model_id"]))
        return {"has_pending_serving": pending}

    def _fake_delete(model_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(("delete", model_id))
        return {}

    monkeypatch.setattr(
        browser_api_module, "list_model_version_records", _fake_versions
    )
    monkeypatch.setattr(
        browser_api_module, "list_model_inference_servings", _fake_servings
    )
    monkeypatch.setattr(
        browser_api_module, "check_model_inference_serving_pending", _fake_pending
    )
    monkeypatch.setattr(browser_api_module, "delete_model", _fake_delete)
    return calls


def _delete(*args: str) -> Any:
    return CliRunner().invoke(
        cli_main, ["model", "delete", *args, "--workspace", _WORKSPACE]
    )


def _delete_json(*args: str) -> Any:
    return CliRunner().invoke(
        cli_main,
        ["--json", "model", "delete", *args, "--workspace", _WORKSPACE],
    )


def _serving(name: str, status: int) -> dict[str, Any]:
    return {"name": name, "serving_id": "serving-secret-1", "status": status}


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def test_model_delete_requires_confirmation_before_session_or_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched: list[str] = []
    monkeypatch.setattr(
        model_commands_module,
        "get_web_session",
        lambda: touched.append("session"),
    )
    monkeypatch.setattr(
        model_commands_module,
        "_resolve_model_name",
        lambda *_args, **_kwargs: touched.append("resolve"),
    )
    monkeypatch.setattr(
        browser_api_module,
        "delete_model",
        lambda *_args, **_kwargs: touched.append("delete"),
    )

    result = _delete_json("qwen-demo")

    assert result.exit_code == 12, result.output
    assert json.loads(result.output) == {
        "success": False,
        "error": {
            "type": "ConfirmationRequired",
            "code": 12,
            "message": "Model deletion requires confirmation.",
            "hint": "Pass --yes to confirm.",
        },
    }
    assert touched == []


def test_model_delete_prompts_and_a_refused_prompt_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_runtime(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        cli_main,
        ["model", "delete", "qwen-demo", "--workspace", _WORKSPACE],
        input="n\n",
    )

    assert result.exit_code != 0
    assert "qwen-demo" in result.output
    assert calls == []


# ---------------------------------------------------------------------------
# The deployment check
# ---------------------------------------------------------------------------


def test_model_delete_checks_deployments_before_it_deletes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_runtime(monkeypatch, tmp_path)

    result = _delete("qwen-demo", "--yes")

    assert result.exit_code == 0, result.output
    assert result.output == "OK Model deleted: qwen-demo\n"
    assert _MODEL_ID not in result.output
    assert [name for name, _ in calls] == [
        "resolve",
        "versions",
        "servings",
        "pending",
        "delete",
    ]
    assert calls[-1] == ("delete", _MODEL_ID)


def test_model_delete_asks_about_every_version_not_only_the_latest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_runtime(monkeypatch, tmp_path, versions=_versions(1, 2, 3))

    result = _delete("qwen-demo", "--yes")

    assert result.exit_code == 0, result.output
    # Deletion takes every version's deployments with it, so every version has
    # to be asked about -- not just the one `model status` reports on.
    assert [value for name, value in calls if name == "servings"] == [1, 2, 3]


def test_model_delete_refuses_a_model_a_serving_still_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_runtime(
        monkeypatch,
        tmp_path,
        versions=_versions(1, 2),
        servings={
            1: [_serving("chat-api", _RUNNING)],
            2: [_serving("batch-api", _STOPPED)],
        },
    )

    result = _delete_json("qwen-demo", "--yes")

    assert result.exit_code == 12, result.output
    error = json.loads(result.output)["error"]
    assert error["type"] == "ValidationError"
    assert "V1 chat-api (RUNNING)" in error["message"]
    assert "V2 batch-api (STOPPED)" in error["message"]
    assert "--force" in error["hint"]
    assert "serving-secret-1" not in result.output
    assert _MODEL_ID not in result.output
    assert "delete" not in [name for name, _ in calls]


def test_model_delete_ignores_a_failed_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_runtime(
        monkeypatch,
        tmp_path,
        servings={1: [_serving("dead-api", _FAILED)]},
    )

    result = _delete("qwen-demo", "--yes")

    # A failed serving is not running and cannot be started, so it holds
    # nothing and must not block the delete.
    assert result.exit_code == 0, result.output
    assert ("delete", _MODEL_ID) in calls


def test_model_delete_refuses_a_model_with_a_queued_deployment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_runtime(monkeypatch, tmp_path, pending=True)

    result = _delete_json("qwen-demo", "--yes")

    assert result.exit_code == 12, result.output
    error = json.loads(result.output)["error"]
    # A deployment that is queued rather than running shows up in no serving
    # list and in no version's running count; this is the only signal for it.
    assert "a deployment is queued on this model" in error["message"]
    assert "delete" not in [name for name, _ in calls]


def test_model_delete_refuses_when_the_deployment_check_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        browser_api_module,
        "list_model_version_records",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("gateway said 503 for model_id=model-secret-123")
        ),
    )

    result = _delete_json("qwen-demo", "--yes")

    # The platform failing to answer is not the platform answering "nothing
    # uses it": a model whose deployments were never seen is not deletable.
    assert result.exit_code == 13, result.output
    error = json.loads(result.output)["error"]
    assert error["message"] == "Could not check which deployments still use this model."
    assert "--force" in error["hint"]
    assert "503" not in result.output
    assert _MODEL_ID not in result.output
    assert "delete" not in [name for name, _ in calls]


def test_model_delete_force_skips_the_deployment_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_runtime(
        monkeypatch,
        tmp_path,
        servings={1: [_serving("chat-api", _RUNNING)]},
        pending=True,
    )

    result = _delete("qwen-demo", "--yes", "--force")

    assert result.exit_code == 0, result.output
    assert [name for name, _ in calls] == ["resolve", "delete"]


def test_model_delete_bounds_the_names_it_lists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_runtime(
        monkeypatch,
        tmp_path,
        servings={1: [_serving(f"api-{index:02d}", _RUNNING) for index in range(25)]},
    )

    result = _delete_json("qwen-demo", "--yes")

    assert result.exit_code == 12, result.output
    message = json.loads(result.output)["error"]["message"]
    assert message.count("V1 api-") == 20
    assert "and 5 more" in message


# ---------------------------------------------------------------------------
# Output boundary
# ---------------------------------------------------------------------------


def test_model_delete_json_is_name_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_runtime(monkeypatch, tmp_path)

    result = _delete_json("qwen-demo", "--yes")

    assert result.exit_code == 0, result.output
    assert _json_data(result.output) == {"name": "qwen-demo", "status": "deleted"}
    assert _MODEL_ID not in result.output


def test_model_delete_error_hides_the_internal_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        browser_api_module,
        "delete_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "request payload failed at /Users/alice/private.log "
                f"model_id={_MODEL_ID}"
            )
        ),
    )

    result = _delete_json("qwen-demo", "--yes")

    assert result.exit_code == 13, result.output
    assert json.loads(result.output)["error"]["message"] == "Could not delete model."
    assert "/Users/alice/private.log" not in result.output
    assert _MODEL_ID not in result.output


def test_model_delete_rejects_a_handle_at_the_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_runtime(monkeypatch, tmp_path)

    result = _delete_json("model-11111111-1111-1111-1111-111111111111", "--yes")

    assert result.exit_code == 12, result.output
    assert json.loads(result.output)["error"]["message"] == (
        "CLI commands only accept model names."
    )
    assert calls == []


# ---------------------------------------------------------------------------
# Wrapper
# ---------------------------------------------------------------------------


def _install_fake_request(
    monkeypatch: pytest.MonkeyPatch, response: dict, record: dict
) -> None:
    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):  # noqa: ANN001
        record.update(
            {
                "method": method,
                "url": url,
                "referer": referer,
                "body": body,
                "timeout": timeout,
            }
        )
        return response

    monkeypatch.setattr(models_module, "_request_json", _fake)


class _FakeSession:
    workspace_id = "ws-default"


def test_delete_model_posts_only_the_model_id(monkeypatch: pytest.MonkeyPatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"Result": {}}, record)

    result = models_module.delete_model(
        "model-9",
        session=_FakeSession(),
        workspace_id="ws-1",
    )

    assert result == {}
    assert record["method"] == "POST"
    # The route is the hyphenated spelling; the underscore form 404s.
    assert record["url"] == "/api/v2/model-hub?Action=DeleteModel"
    assert record["body"] == {"model_id": "model-9"}
    assert str(record["referer"]).endswith("spaceId=ws-1")


def test_delete_model_raises_what_the_platform_said(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "ResponseMetadata": {
                "Error": {"Code": "AccessForbidden", "Message": "Access denied"}
            }
        },
        record,
    )

    with pytest.raises(ValueError, match="Access denied"):
        models_module.delete_model("model-9", session=_FakeSession())


def _model_group_commands() -> set[str]:
    from inspire.cli.commands.model import model as model_group

    return set(model_group.commands)


def test_model_hub_exposes_no_model_editing_entry_point() -> None:
    # `model-hub.UpdateModel` is closed to ordinary users: a freshly created,
    # self-owned model answers `AccessForbidden` for both the `model_id` and
    # the `id` spelling, so there is nothing to wrap and no CLI surface for it.
    assert not any(
        name.startswith(("update_model", "edit_model"))
        for name in dir(browser_api_module)
    )
    assert not {"update", "edit"} & _model_group_commands()


def test_model_group_registers_delete() -> None:
    assert "delete" in _model_group_commands()


@pytest.mark.parametrize("flag", ["--yes", "--force", "--pick", "--project"])
def test_model_delete_help_documents_its_options(flag: str) -> None:
    result = CliRunner().invoke(cli_main, ["model", "delete", "--help"])

    assert result.exit_code == 0, result.output
    assert flag in result.output
    assert "model_id" not in result.output


def test_model_delete_help_is_explicit_about_scope() -> None:
    result = CliRunner().invoke(cli_main, ["model", "delete", "--help"])
    output = " ".join(result.output.split())

    assert "cannot be undone" in output
    assert "not version-scoped" in output
    # Nothing is removed from shared storage; the entry can be registered again.
    assert "shared storage is left alone" in output
