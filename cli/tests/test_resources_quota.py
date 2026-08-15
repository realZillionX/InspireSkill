"""Unit tests for `inspire resources quota`.

`resources availability` answers "are there free nodes"; this command answers
"is the workspace still allowed to take them". The two fail differently — a
spent quota leaves a task in QUOTA_PENDING while a busy cluster leaves it
PENDING — so the output has to keep the quota ceiling and the physical
capacity visibly apart.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.resources import resources_quota as quota_module
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api.workspaces import (
    UNLIMITED_QUOTA,
    WorkspaceQuotaUsage,
)


class _FakeSession:
    storage_state: dict[str, Any] = {}
    workspace_id = "ws-gpu"
    all_workspace_names = {"ws-gpu": "分布式训练空间", "ws-cpu": "CPU资源空间"}
    all_workspace_ids = ["ws-gpu", "ws-cpu"]


_USAGE = [
    WorkspaceQuotaUsage(
        resource="gpu",
        limit=10000,
        used=4682,
        capacity=5597,
        capacity_used=5452,
    ),
    WorkspaceQuotaUsage(
        resource="cpu",
        limit=UNLIMITED_QUOTA,
        used=93583,
        capacity=126607,
        capacity_used=107852,
    ),
    WorkspaceQuotaUsage(
        resource="memory_gib",
        limit=UNLIMITED_QUOTA,
        used=1045550,
        capacity=1323666.07,
        capacity_used=1201978,
    ),
]


def _patch_cli(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    config = config_module.Config(username="user", password="pass")
    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(quota_module, "get_web_session", lambda: _FakeSession())

    def _fake(workspace_id, *, session, priority="high"):
        calls.append({"workspace_id": workspace_id, "priority": priority})
        return _USAGE

    monkeypatch.setattr(quota_module, "get_workspace_quota_usage", _fake)
    return calls


def test_quota_table_separates_the_ceiling_from_the_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli(monkeypatch)

    result = CliRunner().invoke(
        cli_main, ["resources", "quota", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0, result.output
    assert "Quota Limit" in result.output
    assert "Cluster Used/Total" in result.output
    assert "5318" in result.output  # 10000 - 4682 still available under quota
    assert "5452/5597" in result.output


def test_quota_reports_an_absent_ceiling_as_unlimited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli(monkeypatch)

    result = CliRunner().invoke(
        cli_main, ["resources", "quota", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0, result.output
    # `-1` is the platform's spelling of "no ceiling"; printing it raw would
    # read as a negative allowance.
    assert "-1" not in result.output
    assert "unlimited" in result.output


def test_quota_json_is_name_only_and_handle_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli(monkeypatch)

    result = CliRunner().invoke(
        cli_main, ["--json", "resources", "quota", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["priority"] == "high"
    gpu = data["items"][0]
    assert gpu["workspace"] == "分布式训练空间"
    assert gpu["resource"] == "gpu"
    assert gpu["limit"] == 10000
    assert gpu["available"] == 5318
    assert gpu["unlimited"] is False
    assert "ws-gpu" not in result.output

    cpu = data["items"][1]
    assert cpu["unlimited"] is True
    # An unlimited ceiling has no remainder to report, so the keys are absent
    # rather than misleadingly present.
    assert "limit" not in cpu
    assert "available" not in cpu


def test_quota_low_priority_is_a_separate_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_cli(monkeypatch)

    result = CliRunner().invoke(
        cli_main,
        ["resources", "quota", "--workspace", "分布式训练空间", "--priority", "low"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [{"workspace_id": "ws-gpu", "priority": "low"}]


def test_quota_refuses_a_workspace_fanout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quota ceiling belongs to one workspace, so there is nothing to fan out."""
    calls = _patch_cli(monkeypatch)

    result = CliRunner().invoke(
        cli_main, ["resources", "quota", "--workspace", "all", "--all"]
    )

    assert result.exit_code != 0
    assert "--workspace requires one workspace name for this command." in result.output
    assert calls == []


def test_quota_limit_and_all_conflict_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        quota_module,
        "get_workspace_quota_usage",
        lambda *_args, **_kwargs: pytest.fail("budget conflict must fail first"),
    )

    result = CliRunner().invoke(
        cli_main,
        ["resources", "quota", "--workspace", "分布式训练空间", "--limit", "5", "--all"],
    )

    assert result.exit_code != 0


def test_quota_workspace_metavar_is_one_name() -> None:
    option = {
        parameter.name: parameter
        for parameter in quota_module.quota_resources.params
    }["workspace"]

    assert option.metavar == "NAME"
