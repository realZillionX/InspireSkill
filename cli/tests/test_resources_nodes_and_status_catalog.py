from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api import FullFreeNodeCount, GPUAvailability, JobInfo


_WS_DEFAULT = "ws-00000000-0000-0000-0000-0000000000aa"


class _Session:
    workspace_id = _WS_DEFAULT
    all_workspace_ids = [_WS_DEFAULT]
    all_workspace_names = {_WS_DEFAULT: "Default WS"}


def _patch_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = config_module.Config(
        username="user",
        password="pass",
        base_url="https://qz.sii.edu.cn",
        log_cache_dir=str(tmp_path / "logs"),
    )
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **kwargs: (cfg, {})),
    )


def test_resources_nodes_filters_and_returns_json_recommendation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_nodes as nodes_module

    monkeypatch.setattr(nodes_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        nodes_module.browser_api_module,
        "get_accurate_resource_availability",
        lambda **_: [
            GPUAvailability(
                group_id="cg-11111111-1111-1111-1111-111111111111",
                group_name="H200-2号机房",
                gpu_type="NVIDIA_H200",
                total_gpus=64,
                used_gpus=16,
                available_gpus=48,
                low_priority_gpus=0,
                workspace_id=_WS_DEFAULT,
                workspace_name="Default WS",
            ),
            GPUAvailability(
                group_id="cg-22222222-2222-2222-2222-222222222222",
                group_name="H200-1号机房",
                gpu_type="NVIDIA_H200",
                total_gpus=64,
                used_gpus=56,
                available_gpus=8,
                low_priority_gpus=0,
                workspace_id=_WS_DEFAULT,
                workspace_name="Default WS",
            ),
        ],
    )
    monkeypatch.setattr(
        nodes_module.browser_api_module,
        "get_full_free_node_counts",
        lambda group_ids, gpu_per_node: [
            FullFreeNodeCount(
                group_id="cg-11111111-1111-1111-1111-111111111111",
                group_name="H200-2号机房",
                gpu_per_node=gpu_per_node,
                total_nodes=8,
                ready_nodes=8,
                full_free_nodes=6,
            ),
            FullFreeNodeCount(
                group_id="cg-22222222-2222-2222-2222-222222222222",
                group_name="H200-1号机房",
                gpu_per_node=gpu_per_node,
                total_nodes=8,
                ready_nodes=8,
                full_free_nodes=1,
            ),
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "resources", "nodes", "--min-full-free-nodes", "2"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    data = payload["data"]
    assert [row["group_name"] for row in data["groups"]] == ["H200-2号机房"]
    assert data["recommendation"]["group_name"] == "H200-2号机房"
    assert data["recommendation"]["full_free_nodes"] == 6
    assert data["min_full_free_nodes"] == 2


def test_resources_nodes_human_scrubs_raw_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.resources import resources_nodes as nodes_module

    raw_group_id = "cg-11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(nodes_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        nodes_module.browser_api_module,
        "get_accurate_resource_availability",
        lambda **_: [
            GPUAvailability(
                group_id=raw_group_id,
                group_name=f"H200 {raw_group_id}",
                gpu_type="NVIDIA_H200",
                total_gpus=64,
                used_gpus=16,
                available_gpus=48,
                low_priority_gpus=0,
                workspace_id=_WS_DEFAULT,
                workspace_name="Default WS",
            )
        ],
    )
    monkeypatch.setattr(
        nodes_module.browser_api_module,
        "get_full_free_node_counts",
        lambda group_ids, gpu_per_node: [
            FullFreeNodeCount(
                group_id=raw_group_id,
                group_name=f"H200 {raw_group_id}",
                gpu_per_node=gpu_per_node,
                total_nodes=8,
                ready_nodes=8,
                full_free_nodes=6,
            )
        ],
    )

    result = CliRunner().invoke(cli_main, ["resources", "nodes", "--min-full-free-nodes", "2"])

    assert result.exit_code == 0, result.output
    assert raw_group_id not in result.output
    assert "<raw-id>" in result.output
    assert "Recommended:" in result.output


def _job(*, job_id: str, name: str, status: str) -> JobInfo:
    return JobInfo(
        job_id=job_id,
        name=name,
        status=status,
        command="bash train.sh",
        created_at="2026-05-09T00:00:00Z",
        finished_at=None,
        created_by_name="User",
        created_by_id="user-1",
        project_id="proj-1",
        project_name="Project",
        compute_group_name="H200",
        gpu_type="H200",
        gpu_count=8,
        instance_count=1,
        priority=10,
        workspace_id=_WS_DEFAULT,
    )


def test_job_status_catalog_groups_unknown_statuses_without_job_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.job import job_commands as job_module

    calls: list[int] = []

    def list_jobs(**kwargs):
        calls.append(kwargs["page_num"])
        if kwargs["page_num"] == 1:
            return (
                [
                    _job(
                        job_id="job-11111111-1111-1111-1111-111111111111",
                        name="train-a",
                        status="job_running",
                    ),
                    _job(
                        job_id="job-22222222-2222-2222-2222-222222222222",
                        name="train-b",
                        status="job_pausing",
                    ),
                ],
                3,
            )
        if kwargs["page_num"] == 2:
            return (
                [
                    _job(
                        job_id="job-33333333-3333-3333-3333-333333333333",
                        name="train-c",
                        status="job_pausing",
                    )
                ],
                3,
            )
        return ([], 3)

    monkeypatch.setattr(job_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        job_module.browser_api_module,
        "get_current_user",
        lambda **_: {"id": "user-1"},
    )
    monkeypatch.setattr(job_module.browser_api_module, "list_jobs", list_jobs)

    result = CliRunner().invoke(
        cli_main,
        ["job", "status-catalog", "--page-size", "2", "--max-pages", "5"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [1, 2]
    assert "unknown" in result.output
    assert "job_pausing" in result.output
    assert "2" in result.output
    assert "job-11111111-1111-1111-1111-111111111111" not in result.output


def test_job_status_catalog_json_includes_known_and_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)

    from inspire.cli.commands.job import job_commands as job_module

    monkeypatch.setattr(job_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        job_module.browser_api_module,
        "get_current_user",
        lambda **_: {"id": "user-1"},
    )
    monkeypatch.setattr(
        job_module.browser_api_module,
        "list_jobs",
        lambda **_: (
            [
                _job(
                    job_id="job-11111111-1111-1111-1111-111111111111",
                    name="train-a",
                    status="job_running",
                ),
                _job(
                    job_id="job-22222222-2222-2222-2222-222222222222",
                    name="train-b",
                    status="job_drifting",
                ),
            ],
            2,
        ),
    )

    result = CliRunner().invoke(cli_main, ["--json", "job", "status-catalog"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    statuses = {row["status"]: row for row in payload["data"]["statuses"]}
    assert statuses["job_running"]["name"] == "known"
    assert statuses["job_drifting"]["name"] == "unknown"
    assert payload["data"]["unknown_statuses"] == [statuses["job_drifting"]]
