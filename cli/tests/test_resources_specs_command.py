from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.main import main as cli_main


def _patch_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = config_module.Config(
        username="user",
        password="pass",
        base_url="https://qz.sii.edu.cn",
        job_cache_path=str(tmp_path / "jobs.json"),
        log_cache_dir=str(tmp_path / "logs"),
    )
    cfg.workspaces = {
        "分布式训练空间": "ws-11111111-1111-1111-1111-111111111111",
        "CPU资源空间":   "ws-22222222-2222-2222-2222-222222222222",
    }
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **kwargs: (cfg, {})),
    )


_WS_DEFAULT = "ws-00000000-0000-0000-0000-0000000000aa"
_WS_CPU = "ws-22222222-2222-2222-2222-222222222222"
_WS_TRAIN = "ws-11111111-1111-1111-1111-111111111111"


class _Session:
    """Minimal stand-in for WebSession with a multi-workspace account."""

    workspace_id = _WS_DEFAULT
    all_workspace_ids = [_WS_DEFAULT, _WS_CPU, _WS_TRAIN]
    all_workspace_names = {
        _WS_DEFAULT: "Default WS",
        _WS_CPU: "CPU资源空间",
        _WS_TRAIN: "分布式训练空间",
    }


def _stub_browser(
    monkeypatch: pytest.MonkeyPatch,
    *,
    groups_by_ws: dict[str, list[dict]],
    prices_fn,
):
    from inspire.cli.commands.resources import resources_specs as specs_module

    monkeypatch.setattr(specs_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        specs_module.browser_api_module,
        "list_notebook_compute_groups",
        lambda **kwargs: groups_by_ws.get(kwargs["workspace_id"], []),
    )
    monkeypatch.setattr(
        specs_module.browser_api_module, "get_resource_prices", prices_fn
    )


def _make_price(*, qid: str, gpu: int, cpu: int, mem: int, gpu_type: str = "") -> dict:
    return {
        "quota_id": qid,
        "cpu_count": cpu,
        "memory_size_gib": mem,
        "gpu_count": gpu,
        "gpu_info": {"gpu_type_display": gpu_type or "CPU"},
    }


def test_default_usage_is_all_and_sweeps_every_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    queried_workspaces: list[str] = []

    def prices(**kwargs):
        return []

    def list_groups(**kwargs):
        queried_workspaces.append(kwargs["workspace_id"])
        return []

    from inspire.cli.commands.resources import resources_specs as specs_module

    monkeypatch.setattr(specs_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        specs_module.browser_api_module, "list_notebook_compute_groups", list_groups
    )
    monkeypatch.setattr(specs_module.browser_api_module, "get_resource_prices", prices)

    result = CliRunner().invoke(cli_main, ["--json", "resources", "specs"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["data"]["usage_filter"] == "all"
    # All workspaces hit, not just default.
    assert sorted(queried_workspaces) == sorted([_WS_DEFAULT, _WS_CPU, _WS_TRAIN])


def test_explicit_workspace_skips_cross_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    queried: list[str] = []

    def list_groups(**kwargs):
        queried.append(kwargs["workspace_id"])
        return []

    from inspire.cli.commands.resources import resources_specs as specs_module

    monkeypatch.setattr(specs_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        specs_module.browser_api_module, "list_notebook_compute_groups", list_groups
    )
    monkeypatch.setattr(
        specs_module.browser_api_module, "get_resource_prices", lambda **_: []
    )

    result = CliRunner().invoke(
        cli_main, ["--json", "resources", "specs", "--workspace", "CPU资源空间"]
    )
    assert result.exit_code == 0
    assert queried == [_WS_CPU]


def test_default_usage_all_queries_all_three_schedule_types(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    seen_types: list[str] = []

    def prices(**kwargs):
        seen_types.append(kwargs["schedule_config_type"])
        if kwargs["schedule_config_type"] == "SCHEDULE_CONFIG_TYPE_DSW":
            return [_make_price(qid="q-dsw", gpu=0, cpu=4, mem=16)]
        if kwargs["schedule_config_type"] == "SCHEDULE_CONFIG_TYPE_HPC":
            return [_make_price(qid="q-hpc", gpu=0, cpu=8, mem=32)]
        return [_make_price(qid="q-ray", gpu=0, cpu=2, mem=8)]

    _stub_browser(
        monkeypatch,
        groups_by_ws={_WS_CPU: [{"logic_compute_group_id": "lcg-1", "name": "CPU资源-2"}]},
        prices_fn=prices,
    )
    result = CliRunner().invoke(
        cli_main, ["--json", "resources", "specs", "--workspace", "CPU资源空间"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    rows_by_usage = {row["usage"]: row for row in payload["data"]["specs"]}
    assert set(rows_by_usage) == {"notebook", "hpc", "ray"}
    assert {row["spec_id"] for row in payload["data"]["specs"]} == {
        "q-dsw", "q-hpc", "q-ray"
    }
    assert sorted(set(seen_types)) == sorted(
        [
            "SCHEDULE_CONFIG_TYPE_DSW",
            "SCHEDULE_CONFIG_TYPE_HPC",
            "SCHEDULE_CONFIG_TYPE_RAY_JOB",
        ]
    )


def test_json_rows_carry_only_names_no_uuids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CLI must not surface workspace_id / logic_compute_group_id to callers."""
    _patch_config(monkeypatch, tmp_path)
    _stub_browser(
        monkeypatch,
        groups_by_ws={_WS_CPU: [{"logic_compute_group_id": "lcg-secret", "name": "CPU资源-2"}]},
        prices_fn=lambda **_: [_make_price(qid="q-1", gpu=0, cpu=4, mem=16)],
    )
    result = CliRunner().invoke(
        cli_main, ["--json", "resources", "specs", "--workspace", "CPU资源空间"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    # Top-level: only names, never IDs.
    assert "workspace_id" not in payload["data"]
    assert "workspace_ids" not in payload["data"]
    assert "workspace_names" in payload["data"]
    # Per row: spec_id is the only ID we forward (callers feed it to ray create).
    row = payload["data"]["specs"][0]
    assert row.keys() == {
        "workspace_name",
        "usage",
        "compute_group_name",
        "spec_id",
        "cpu_count",
        "memory_size_gib",
        "gpu_count",
        "gpu_type",
    }
    assert "workspace_id" not in row
    assert "logic_compute_group_id" not in row
    assert "schedule_config_type" not in row
    # And the leaked-looking lcg id must not appear anywhere in the JSON.
    assert "lcg-secret" not in result.output


def test_usage_ray_only_queries_ray(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    seen_types: list[str] = []

    def prices(**kwargs):
        seen_types.append(kwargs["schedule_config_type"])
        return [_make_price(qid="q-r", gpu=0, cpu=4, mem=16)]

    _stub_browser(
        monkeypatch,
        groups_by_ws={_WS_CPU: [{"logic_compute_group_id": "lcg-1", "name": "CPU资源-2"}]},
        prices_fn=prices,
    )
    result = CliRunner().invoke(
        cli_main,
        ["--json", "resources", "specs", "--workspace", "CPU资源空间", "--usage", "ray"],
    )
    assert result.exit_code == 0
    assert seen_types == ["SCHEDULE_CONFIG_TYPE_RAY_JOB"]


def test_group_filter_skips_non_matching_compute_groups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(monkeypatch, tmp_path)
    queried_groups: list[str] = []

    def prices(**kwargs):
        queried_groups.append(kwargs["logic_compute_group_id"])
        return [_make_price(qid="q-1", gpu=0, cpu=4, mem=16)]

    _stub_browser(
        monkeypatch,
        groups_by_ws={
            _WS_CPU: [
                {"logic_compute_group_id": "lcg-cpu-1", "name": "CPU资源-1"},
                {"logic_compute_group_id": "lcg-cpu-2", "name": "CPU资源-2"},
                {"logic_compute_group_id": "lcg-hpc-2", "name": "HPC-可上网区资源-2"},
            ]
        },
        prices_fn=prices,
    )
    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "resources",
            "specs",
            "--workspace",
            "CPU资源空间",
            "--group",
            "HPC",
            "--usage",
            "ray",
        ],
    )
    assert result.exit_code == 0
    # Only the HPC-named group should be queried.
    assert queried_groups == ["lcg-hpc-2"]


def test_help_advertises_default_all_and_no_uuid_in_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = CliRunner().invoke(cli_main, ["resources", "specs", "--help"])
    assert result.exit_code == 0
    assert "Sweep" in result.output or "sweep" in result.output
    # Help must not refer to UUIDs as something the user reads off the table.
    assert "logic_compute_group_id" not in result.output
    assert "workspace_id" not in result.output
    # The four valid usage values stay listed.
    for u in ("all", "notebook", "hpc", "ray"):
        assert u in result.output


def test_no_specs_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_config(monkeypatch, tmp_path)
    _stub_browser(monkeypatch, groups_by_ws={}, prices_fn=lambda **_: [])
    result = CliRunner().invoke(
        cli_main, ["resources", "specs", "--workspace", "CPU资源空间"]
    )
    assert result.exit_code == 0
    assert "No resource specs found." in result.output
