"""Regression tests for the optional, read-only group ranking example."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


@pytest.fixture(scope="module")
def picker():
    path = Path(__file__).resolve().parents[2] / "examples" / "agent_ops" / "pick_zone.py"
    spec = importlib.util.spec_from_file_location("agent_ops_pick_zone", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def availability(name="pool-a", *, free=2, high=3, total=0, balance=4, hp=24):
    return {
        "compute_group": name, "kind": "gpu", "gpu_type": "Model-X",
        "gpus_per_node": 8, "free_nodes": free, "full_free_nodes": free,
        "high_priority_free_nodes": high,
        "reclaimable_nodes": high - free, "total_gpus": total,
        "available_gpus": balance, "high_priority_available_gpus": hp,
    }


def quota(name="pool-a", size=8, levels=None):
    return {"compute_group": name, "gpu_type": "Model-X", "quota": f"{size},16,64",
            "allowed_priority_levels": [] if levels is None else levels}


def test_high_nodes_already_include_idle_nodes(picker):
    rows = picker.rank_groups([availability()], [quota()],
                              gpus=32, nodes=4, priority_level="high")
    assert rows[0]["capacity_gpus"] == 24
    assert rows[0]["snapshot_fits"] is False


def test_zero_guarantee_does_not_hide_gpu_pool(picker):
    rows = picker.rank_groups([availability(total=0)], [quota()],
                              gpus=8, nodes=1, priority_level="high")
    assert rows[0]["snapshot_fits"] is True


def test_low_priority_cannot_count_reclaimable_nodes(picker):
    rows = picker.rank_groups([availability(free=0, high=10)], [quota()],
                              gpus=8, nodes=1, priority_level="low")
    assert rows[0]["capacity_gpus"] == 0
    assert rows[0]["snapshot_fits"] is False


def test_low_priority_fragments_do_not_use_high_priority_balance(picker):
    rows = picker.rank_groups([availability(balance=-2, hp=100)], [quota(size=2)],
                              gpus=2, nodes=1, priority_level="low")
    assert rows[0]["capacity_gpus"] == -2
    assert not rows[0]["snapshot_fits"]


@pytest.mark.parametrize("gpus", [1, 8])
def test_priority_restrictions_apply_to_all_instance_sizes(picker, gpus):
    rows = picker.rank_groups([availability()], [quota(size=gpus, levels=["low"])],
                              gpus=gpus, nodes=1, priority_level="high")
    assert rows[0]["eligible"] == "blocked"


def test_unknown_priority_is_not_treated_as_unrestricted(picker):
    row = quota()
    row["allowed_priority_levels"] = None
    rows = picker.rank_groups([availability()], [row],
                              gpus=8, nodes=1, priority_level="high")
    assert rows[0]["eligible"] == "unknown"
    assert rows[0]["matching_quotas"] == ["8,16,64"]


def test_quota_rows_not_group_names_decide_eligibility(picker):
    groups = [availability("alpha"), availability("beta", high=20)]
    quotas = [quota("alpha"), quota("beta", levels=["low"])]
    rows = picker.rank_groups(groups, quotas, gpus=8, nodes=1, priority_level="high")
    assert rows[0]["group"] == "alpha"
    assert rows[1]["eligible"] == "blocked"


def test_live_six_gpu_row_accepted_for_single_instance(picker):
    rows = picker.rank_groups([availability(hp=10)], [quota(size=6)],
                              gpus=6, nodes=1, priority_level="high")
    assert rows[0]["eligible"] == "allowed"
    assert rows[0]["snapshot_fits"]


def test_hardware_and_memory_requirements_are_not_silently_lowered(picker):
    assert not picker.rank_groups([availability()], [quota()], gpus=8, nodes=1,
                                  priority_level="high", gpu_type="other-model")
    rows = picker.rank_groups([availability()], [quota()], gpus=8, nodes=1,
                              priority_level="high", min_memory_gib=128)
    assert rows[0]["eligible"] == "blocked"


def test_missing_capacity_is_unknown_not_zero(picker):
    row = availability()
    del row["high_priority_free_nodes"]
    result = picker.rank_groups([row], [quota()], gpus=8, nodes=1, priority_level="high")
    assert result[0]["capacity_gpus"] is None
    assert not result[0]["snapshot_fits"]


@pytest.mark.parametrize("size", [4, 6, 16])
def test_unsupported_multinode_layout_is_not_certified(picker, size):
    with pytest.raises(picker.QueryError, match="8-GPU"):
        picker.rank_groups([availability(hp=500)], [quota(size=size)],
                           gpus=size * 4, nodes=4, priority_level="high")


def test_mixed_nodes_do_not_count_small_idle_nodes_as_full(picker):
    row = availability(free=2, high=0)
    row["full_free_nodes"] = 0
    result = picker.rank_groups([row], [quota()], gpus=8, nodes=1, priority_level="low")
    assert result[0]["capacity_gpus"] == 0
    assert not result[0]["snapshot_fits"]


def test_whole_node_counters_do_not_use_maximum_node_size(picker):
    row = availability(free=1, high=2)
    row["gpus_per_node"] = 16
    result = picker.rank_groups([row], [quota()], gpus=32, nodes=4, priority_level="high")
    assert result[0]["capacity_gpus"] == 16
    assert not result[0]["snapshot_fits"]


def test_success_json_and_stderr_warning_remain_separate(picker, monkeypatch):
    monkeypatch.setattr(picker.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, json.dumps({"success": True, "data": {"items": [quota()]}}),
        "ACL warning: could not verify local permissions\n",
    ))
    assert picker.query("fake", ["job", "quota"], None) == [quota()]


def test_nonzero_exit_never_becomes_valid_empty_capacity(picker, monkeypatch):
    monkeypatch.setattr(picker.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 10, json.dumps({"success": True, "data": {"items": []}}), "",
    ))
    with pytest.raises(picker.QueryError) as error:
        picker.query("fake", [], None)
    assert error.value.code == 10


def test_stderr_config_error_is_preserved(picker, monkeypatch):
    stderr = "warning\n" + json.dumps({"success": False, "error": {
        "type": "ConfigError", "message": "workspace missing",
    }})
    monkeypatch.setattr(picker.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 10, "", stderr,
    ))
    with pytest.raises(picker.QueryError, match="ConfigError: workspace missing"):
        picker.query("fake", [], None)


@pytest.mark.parametrize("stdout", [
    "{}", '{"success": true, "data": {}}',
    '{"success":true,"data":{"items":[]}}\nextra',
    '{"success":true,"data":{"items":[],"truncated":true}}',
    '{"success":true,"data":{"items":[],"has_more":true}}',
])
def test_malformed_or_incomplete_snapshot_is_explicit_failure(picker, monkeypatch, stdout):
    monkeypatch.setattr(picker.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout, "",
    ))
    with pytest.raises(picker.QueryError):
        picker.query("fake", [], None)


def test_top_match_after_first_twenty_rows_and_account_pinned(picker, monkeypatch, capsys):
    calls = []
    groups = [availability(f"group-{i}", free=0, high=i) for i in range(25)]
    quotas = [quota(f"group-{i}") for i in range(25)]
    monkeypatch.setattr(picker.shutil, "which", lambda _: "inspire-on-path")

    def run(command, **kwargs):
        calls.append(command)
        data = groups if "availability" in command else quotas
        payload = {"items": data} if "--all" in command else {
            "items": data[:20], "truncated": True, "total": len(data), "shown": 20,
        }
        return subprocess.CompletedProcess(command, 0, json.dumps({
            "success": True, "data": payload,
        }), "warning on stderr\n")

    monkeypatch.setattr(picker.subprocess, "run", run)
    rc = picker.main(["--workspace", "workspace-x", "--account", "account-x",
                      "--gpus", "80", "--nodes", "10",
                      "--priority-level", "high", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result["recommended"]["group"] == "group-24"
    assert len(calls) == 2
    assert calls[0][:4] == ["inspire-on-path", "--json", "--account", "account-x"]
    assert all(command[-1] == "--all" for command in calls)
    assert "create" not in calls[0] and "create" not in calls[1]


def test_no_match_json_has_nonzero_exit(picker, monkeypatch, capsys):
    monkeypatch.setattr(picker.shutil, "which", lambda _: "fake")
    monkeypatch.setattr(picker, "query", lambda *a: [])
    assert picker.main(["--workspace", "w", "--gpus", "1",
                        "--priority-level", "high", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["success"] is True
    assert result["recommended"] is None


def test_cli_missing_is_structured_error(picker, monkeypatch, capsys):
    monkeypatch.setattr(picker.shutil, "which", lambda _: None)
    assert picker.main(["--workspace", "w", "--gpus", "1",
                        "--priority-level", "high", "--json"]) == 127
    assert json.loads(capsys.readouterr().out)["success"] is False


def test_help_does_not_query_platform(picker, monkeypatch):
    def unexpected(*args):
        pytest.fail("help attempted a platform query")

    monkeypatch.setattr(picker, "query", unexpected)
    with pytest.raises(SystemExit) as exit_info:
        picker.main(["--help"])
    assert exit_info.value.code == 0
