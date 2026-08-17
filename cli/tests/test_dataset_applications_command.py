"""`inspire dataset applications`: the two listings, the detail, and the budget."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from inspire.cli.commands.dataset import dataset_commands as dataset_commands_module
from inspire.cli.main import main as cli_main
from inspire.platform.web import plaza as plaza_module
from inspire.platform.web.session import SessionExpiredError


class _Session:
    workspace_id = "ws-11111111-1111-1111-1111-111111111111"
    all_workspace_ids = [workspace_id]
    all_workspace_names = {workspace_id: "训练空间"}
    storage_state: dict[str, Any] = {}


def _application(**overrides: Any) -> plaza_module.DatasetApplication:
    fields: dict[str, Any] = {
        "dataset": "pixabay-81k",
        "state": "pending",
        "authority": "只读",
        "applicant": "张三",
        "project": "多模态基础架构",
        "reason": "训练视频生成模型需要这份数据。",
        "approver": "",
        "applied_at": "2026-08-01 09:12:00",
        "decided_at": "",
        "application_id": 42,
    }
    fields.update(overrides)
    return plaza_module.DatasetApplication(**fields)


def _patch_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dataset_commands_module,
        "require_web_session",
        lambda *_args, **_kwargs: _Session(),
    )


def _patch_lists(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mine: list[plaza_module.DatasetApplication] | None = None,
    incoming: list[plaza_module.DatasetApplication] | None = None,
    total: int | None = None,
) -> list[dict[str, Any]]:
    _patch_session(monkeypatch)
    calls: list[dict[str, Any]] = []

    def _make(rows: list[plaza_module.DatasetApplication], label: str):  # noqa: ANN202
        def _lister(**kwargs: Any) -> tuple[list[plaza_module.DatasetApplication], int]:
            calls.append({"which": label, **kwargs})
            # Page like the plaza does, so `--all` has something to expand.
            page_size = int(kwargs.get("page_size") or len(rows) or 1)
            return rows[:page_size], total if total is not None else len(rows)

        return _lister

    monkeypatch.setattr(
        plaza_module, "list_dataset_applications", _make(mine or [], "mine")
    )
    monkeypatch.setattr(
        plaza_module, "list_dataset_approvals", _make(incoming or [], "incoming")
    )
    return calls


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli_main, ["dataset", "applications", *args])


def _run_json(*args: str) -> Any:
    return CliRunner().invoke(cli_main, ["--json", "dataset", "applications", *args])


def _payload(output: str) -> Any:
    parsed = json.loads(output)
    return parsed.get("data", parsed)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_applications_lists_what_this_account_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_lists(
        monkeypatch,
        mine=[_application(state="approved", approver="李四", decided_at="2026-08-02")],
    )

    result = _run()

    assert result.exit_code == 0, result.output
    assert [call["which"] for call in calls] == ["mine"]
    assert "pixabay-81k" in result.output
    assert "approved" in result.output
    assert "Approver" in result.output


def test_applications_to_approve_uses_the_other_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_lists(monkeypatch, incoming=[_application()])

    result = _run("--to-approve")

    assert result.exit_code == 0, result.output
    assert [call["which"] for call in calls] == ["incoming"]
    # The approver view is the only one that names who asked and for what
    # project, so its table says so.
    assert "Applicant" in result.output
    assert "张三" in result.output
    assert "多模态基础架构" in result.output


def test_an_empty_listing_reads_as_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_lists(monkeypatch)

    mine = _run()
    incoming = _run("--to-approve")

    assert mine.exit_code == 0
    assert "You have not applied for access to any dataset." in mine.output
    assert incoming.exit_code == 0
    assert "No dataset access applications are waiting for you." in incoming.output


def test_applications_passes_the_keyword_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_lists(monkeypatch, mine=[_application()])

    _run("--keyword", "pixabay")

    assert calls[0]["keyword"] == "pixabay"


def test_applications_json_is_name_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_lists(monkeypatch, mine=[_application(state="rejected")])

    result = _run_json()

    assert result.exit_code == 0, result.output
    payload = _payload(result.output)
    assert payload["items"] == [
        {
            "name": "pixabay-81k",
            "state": "rejected",
            "authority": "只读",
            "applied_at": "2026-08-01 09:12:00",
        }
    ]
    # The application's own plaza handle addresses nothing else and never
    # reaches output.
    assert "42" not in result.output
    assert "application_id" not in result.output


# ---------------------------------------------------------------------------
# Output budget
# ---------------------------------------------------------------------------


def test_applications_clips_to_the_collection_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_application(dataset=f"ds-{index:02d}") for index in range(25)]
    _patch_lists(monkeypatch, mine=rows, total=25)

    result = _run_json()

    payload = _payload(result.output)
    assert len(payload["items"]) == 20
    assert payload["shown"] == 20
    assert payload["total"] == 25
    assert payload["truncated"] is True


def test_applications_all_asks_for_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_application(dataset=f"ds-{index:02d}") for index in range(25)]
    calls = _patch_lists(monkeypatch, mine=rows, total=25)

    result = _run_json("--all")

    assert result.exit_code == 0, result.output
    assert len(_payload(result.output)["items"]) == 25
    assert [call["page_size"] for call in calls] == [20, 25]


def test_applications_rejects_limit_with_all(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_lists(monkeypatch, mine=[_application()])

    result = _run_json("--limit", "5", "--all")

    assert result.exit_code == 12, result.output
    assert json.loads(result.output)["error"]["message"] == (
        "Use either --limit or --all, not both."
    )
    assert calls == []


# ---------------------------------------------------------------------------
# Detail by dataset name
# ---------------------------------------------------------------------------


def test_a_name_loads_the_full_record(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch)
    calls: list[dict[str, Any]] = []

    def _find(dataset: str, **kwargs: Any) -> list[plaza_module.DatasetApplication]:
        calls.append({"dataset": dataset, **kwargs})
        return [_application(state="approved", approver="李四", decided_at="2026-08-02")]

    monkeypatch.setattr(plaza_module, "find_dataset_applications", _find)

    result = _run("pixabay-81k")

    assert result.exit_code == 0, result.output
    assert calls[0]["dataset"] == "pixabay-81k"
    assert calls[0]["incoming"] is False
    assert "Name: pixabay-81k" in result.output
    assert "State: approved" in result.output
    # The description is the one field that can run long; it is summarized.
    assert "Reason: 训练视频生成模型需要这份数据。" in result.output


def test_a_name_can_search_the_approver_side(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        plaza_module,
        "find_dataset_applications",
        lambda dataset, **kwargs: (
            calls.append({"dataset": dataset, **kwargs}),
            [_application()],
        )[1],
    )

    result = _run("pixabay-81k", "--to-approve")

    assert result.exit_code == 0, result.output
    assert calls[0]["incoming"] is True


def test_an_unknown_name_is_a_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        plaza_module,
        "find_dataset_applications",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            plaza_module.UnknownDatasetApplicationError(
                "No dataset access application for 'pixabay-81k'."
            )
        ),
    )

    result = _run_json("pixabay-81k")

    assert result.exit_code == 12, result.output
    error = json.loads(result.output)["error"]
    assert error["type"] == "ValidationError"
    assert "pixabay-81k" in error["message"]


def test_a_handle_is_rejected_at_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        plaza_module,
        "find_dataset_applications",
        lambda *_args, **_kwargs: pytest.fail("a handle must not reach the plaza"),
    )

    result = _run_json("ws-11111111-1111-1111-1111-111111111111")

    assert result.exit_code == 12, result.output


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def test_a_lapsed_session_is_an_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        plaza_module,
        "list_dataset_applications",
        lambda **_kwargs: (_ for _ in ()).throw(SessionExpiredError("session gone")),
    )

    result = _run_json()

    assert result.exit_code == 11, result.output
    assert json.loads(result.output)["error"]["type"] == "AuthenticationError"


def test_a_platform_failure_hides_its_internals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_session(monkeypatch)
    monkeypatch.setattr(
        plaza_module,
        "list_dataset_applications",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("GET /api/datasetApplyApprove failed for /Users/alice/x.log")
        ),
    )

    result = _run_json()

    assert result.exit_code == 13, result.output
    assert json.loads(result.output)["error"]["message"] == (
        "Could not list dataset access applications."
    )
    assert "/Users/alice/x.log" not in result.output
    assert "datasetApplyApprove" not in result.output


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


def test_the_dataset_group_registers_applications() -> None:
    from inspire.cli.commands.dataset import dataset as dataset_group

    assert "applications" in dataset_group.commands


def test_help_says_the_command_is_read_only() -> None:
    result = CliRunner().invoke(cli_main, ["dataset", "applications", "--help"])
    output = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "[NAME]" in output
    assert "web-only flow" in output
    assert "does not submit, approve, or withdraw anything" in output
    assert "pending" in output and "withdrawn" in output
