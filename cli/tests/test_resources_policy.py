"""Unit tests for `inspire resources policy` and its browser-API wrapper.

The command answers "how long does this workspace let me keep what I took".
Two properties carry the whole value and both are easy to get silently wrong:

* an *absent* policy record must never render as a *permissive* one — the
  platform answers `Result: null` for HPC on workspaces that run no HPC, and
  reading that as "no reclaim, no cap" points the user at the opposite
  decision;
* the gates inside a reclaim rule are load-bearing, because `CPU < 15% AND GPU
  < 15%` reclaims far less work than the same two clauses joined by OR.

The fixtures below are the shapes the live platform actually returns.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.resources import resources_policy as policy_module
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api import schedule_config
from inspire.platform.web.browser_api.schedule_config import (
    WorkloadSchedulePolicy,
    get_workspace_schedule_policy,
)

# `notebook.GetScheduleConfig` — the shared record covering notebook, train and
# Ray. Trimmed of the spec menus, which are megabytes of `quota_id` handles.
_SHARED = {
    "auto_recycle": 1,
    "recycle_config": {
        "gate": "OR",
        "conds": [
            {"gate": "OR", "conds": [{"crit": "GPU", "hrs": 3, "thresh": 15}]},
            {"gate": "OR", "conds": [{"crit": "RUNTIME", "hrs": 18, "thresh": 0}]},
        ],
    },
    "recycle_hour": 0,
    "recycle_rate": 0,
    "recycle_save": 1,
    "recycle_standard": "CPU",
    "timed_shutdown": 0,
    "shutdown_hour": 0,
    "shutdown_minute": 0,
    "shutdown_save": 0,
    "auto_recycle_train": 1,
    "auto_recycle_train_ruleset": (
        '{"gate":"OR","conds":[{"gate":"OR","conds":[{"hrs":3,"crit":"GPU","thresh":40}]}]}'
    ),
    "timed_recycle_train": 0,
    "recycle_train_day": 0,
    "recycle_train_hour": 0,
    "recycle_train_minute": 0,
    "auto_recycle_rayjob": 0,
    "auto_recycle_rayjob_ruleset": "",
    "timed_recycle_rayjob": 0,
    "recycle_rayjob_day": 0,
    "recycle_rayjob_hour": 0,
    "recycle_rayjob_minute": 0,
    # Maintainer-side capability switches and opaque bookkeeping that must not
    # reach any public output.
    "config_id": "c0c400e2-fc72-44ce-b63d-dc38a82372bc",
    "workspace_id": "ws-9dcc0e1f-80a4-4af2-bc2f-0e352e7b17e6",
    "train_enable_slow_detect": True,
    "train_enable_vccl": True,
    "train_enable_specified_nodes": False,
    "train_enable_troubleshoot": False,
    "quota": '[{"id":"03476e23-8faa-4612-b3e4-b8a320a7d8f7","name":"1卡10核"}]',
    "left_time": "-1",
    "open_ssh": False,
    "ssh_limit": 20000,
}

_HPC = {
    "enable_auto_stop": True,
    "auto_stop_ruleset": (
        '{"gate":"OR","conds":[{"gate":"OR","conds":[{"hrs":5,"crit":"CPU","thresh":30}]}]}'
    ),
    "enable_max_running_time": True,
    "max_running_time_days": 14,
    "max_running_time_hours": 0,
    "max_running_time_minutes": 0,
    "predef_node_spec": '[{"id":"f74b36bc","name":"55核500GB"}]',
    "workspace_id": "ws-f9be64cb-9b66-40fb-8172-488abed619bc",
}

_SERVING = {
    "enable_auto_stop": True,
    "items": [
        {
            "auto_stop_ruleset": (
                '{"gate":"OR","conds":[{"gate":"OR","conds":'
                '[{"hrs":5,"crit":"GPU","thresh":20}]}]}'
            ),
            "gpu_count_min": 8,
            "gpu_count_max": 16,
        }
    ],
    "workspace_id": "ws-9dcc0e1f-80a4-4af2-bc2f-0e352e7b17e6",
}


_DEFAULT = object()


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    shared: Any = _DEFAULT,
    hpc: Any = _DEFAULT,
    serving: Any = _DEFAULT,
) -> list[dict[str, Any]]:
    """Answer each of the three Actions and record what was actually sent."""
    calls: list[dict[str, Any]] = []
    payloads = {
        "GetScheduleConfig": _SHARED if shared is _DEFAULT else shared,
        "GetHpcScheduleConfig": _HPC if hpc is _DEFAULT else hpc,
        "GetServingScheduleConfig": _SERVING if serving is _DEFAULT else serving,
    }

    def _fake(session, method, path, *, referer=None, body=None, timeout=30, **kwargs):
        calls.append({"path": path, "referer": referer, "body": body})
        for action, payload in payloads.items():
            if f"Action={action}" in path:
                return {"Result": payload}
        raise AssertionError(f"unexpected Action: {path}")

    monkeypatch.setattr(schedule_config, "_request_json", _fake)
    monkeypatch.setattr(schedule_config, "_get_base_url", lambda: "https://example.test")
    return calls


def _by_workload(policies: list[WorkloadSchedulePolicy]) -> dict[str, WorkloadSchedulePolicy]:
    return {policy.workload: policy for policy in policies}


# ---------------------------------------------------------------------------
# Wrapper
# ---------------------------------------------------------------------------


def test_five_workloads_come_from_three_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch)

    policies = get_workspace_schedule_policy("ws-gpu", session=object())  # type: ignore[arg-type]

    # notebook, train and Ray share one record; HPC and serving are their own.
    assert len(calls) == 3
    assert [policy.workload for policy in policies] == [
        "notebook",
        "job",
        "hpc",
        "ray",
        "serving",
    ]


def test_only_the_notebook_action_takes_a_pascal_case_workspace_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch)

    get_workspace_schedule_policy("ws-gpu", session=object())  # type: ignore[arg-type]

    sent = {call["path"].split("Action=")[1]: call["body"] for call in calls}
    # Spelling is per Action and the wrong one is rejected outright.
    assert sent["GetScheduleConfig"] == {"WorkspaceId": "ws-gpu"}
    assert sent["GetHpcScheduleConfig"] == {"workspace_id": "ws-gpu"}
    assert sent["GetServingScheduleConfig"] == {"workspace_id": "ws-gpu"}


def test_each_route_keeps_its_own_console_referer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch)

    get_workspace_schedule_policy("ws-gpu", session=object())  # type: ignore[arg-type]

    referers = {call["path"].split("?")[0]: call["referer"] for call in calls}
    assert referers["/api/v2/notebook"] == "https://example.test/jobs/interactiveModeling"
    assert referers["/api/v2/hpc"] == "https://example.test/jobs/highPerformanceComputing"
    assert (
        referers["/api/v2/inference_serving"] == "https://example.test/jobs/modelDeployment"
    )


def test_absent_hpc_policy_is_not_an_unlimited_one(monkeypatch: pytest.MonkeyPatch) -> None:
    # Workspaces that run no HPC answer a literal `Result: null`, which the
    # envelope unwrapper hands over as an empty payload.
    _install(monkeypatch, hpc=None)

    hpc = _by_workload(
        get_workspace_schedule_policy("ws-gpu", session=object())  # type: ignore[arg-type]
    )["hpc"]

    assert hpc.configured is False
    # The distinction the whole row exists for: nothing may read as "off".
    assert hpc.auto_reclaim is None
    assert hpc.max_runtime_minutes is None


def test_a_failed_request_raises_instead_of_reporting_no_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule_config, "_get_base_url", lambda: "https://example.test")

    def _fake(session, method, path, **kwargs):
        if "GetHpcScheduleConfig" in path:
            return {
                "ResponseMetadata": {
                    "Error": {"Code": "AccessForbidden", "Message": "Access denied"}
                }
            }
        return {"Result": _SHARED if "GetScheduleConfig" in path else _SERVING}

    monkeypatch.setattr(schedule_config, "_request_json", _fake)

    # "The platform did not answer" must never collapse into "there is no
    # policy" — a user would read that as permission to leave work running.
    with pytest.raises(ValueError, match="AccessForbidden"):
        get_workspace_schedule_policy("ws-gpu", session=object())  # type: ignore[arg-type]


def test_reclaim_rule_keeps_the_runtime_clause_and_the_or_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch)

    notebook = _by_workload(
        get_workspace_schedule_policy("ws-gpu", session=object())  # type: ignore[arg-type]
    )["notebook"]

    assert notebook.reclaim_description == "gpu < 15% for 3h OR runtime > 18h"


def test_an_and_gate_is_not_flattened_into_an_or(monkeypatch: pytest.MonkeyPatch) -> None:
    shared = dict(_SHARED)
    shared["recycle_config"] = {
        "gate": "OR",
        "conds": [
            {
                "gate": "AND",
                "conds": [
                    {"crit": "CPU", "hrs": 3, "thresh": 15},
                    {"crit": "GPU", "hrs": 3, "thresh": 15},
                ],
            }
        ],
    }
    _install(monkeypatch, shared=shared)

    notebook = _by_workload(
        get_workspace_schedule_policy("ws-gpu", session=object())  # type: ignore[arg-type]
    )["notebook"]

    # AND reclaims far less than OR; conflating them misstates the policy.
    assert notebook.reclaim_description == "cpu < 15% for 3h AND gpu < 15% for 3h"


def test_json_string_rulesets_are_parsed_and_empty_ones_are_not_invented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch)

    policies = _by_workload(
        get_workspace_schedule_policy("ws-gpu", session=object())  # type: ignore[arg-type]
    )

    # train's ruleset arrives as a JSON string, Ray's arrives as "".
    assert policies["job"].reclaim_description == "gpu < 40% for 3h"
    assert policies["ray"].reclaim_rule is None
    assert policies["ray"].auto_reclaim is False


def test_the_legacy_three_field_rule_is_read_the_way_the_console_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = dict(_SHARED)
    shared["recycle_config"] = None
    shared["recycle_hour"] = 6
    shared["recycle_standard"] = "GPU"
    shared["recycle_rate"] = 20
    _install(monkeypatch, shared=shared)

    notebook = _by_workload(
        get_workspace_schedule_policy("ws-gpu", session=object())  # type: ignore[arg-type]
    )["notebook"]

    # Without the fallback this workspace would report "no idle reclaim" while
    # the console shows a rule.
    assert notebook.reclaim_description == "gpu < 20% for 6h"


def test_a_partial_legacy_triple_is_not_turned_into_a_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = dict(_SHARED)
    shared["recycle_config"] = {}
    shared["recycle_hour"] = 6
    shared["recycle_standard"] = "GPU"
    shared["recycle_rate"] = 0
    _install(monkeypatch, shared=shared)

    notebook = _by_workload(
        get_workspace_schedule_policy("ws-gpu", session=object())  # type: ignore[arg-type]
    )["notebook"]

    assert notebook.reclaim_rule is None


def test_runtime_caps_are_totalled_across_day_hour_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = dict(_SHARED)
    shared["timed_recycle_train"] = 1
    shared["recycle_train_day"] = 1
    shared["recycle_train_hour"] = 12
    shared["recycle_train_minute"] = 30
    _install(monkeypatch, shared=shared)

    policies = _by_workload(
        get_workspace_schedule_policy("ws-gpu", session=object())  # type: ignore[arg-type]
    )

    assert policies["job"].max_runtime_minutes == 36 * 60 + 30
    assert schedule_config.format_duration(policies["job"].max_runtime_minutes) == "1d 12h 30m"
    # HPC spells the same concept `max_running_time_*`.
    assert policies["hpc"].max_runtime_minutes == 14 * 24 * 60
    assert schedule_config.format_duration(policies["hpc"].max_runtime_minutes) == "14d"


def test_a_disabled_timed_switch_hides_its_leftover_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = dict(_SHARED)
    shared["timed_recycle_train"] = 0
    shared["recycle_train_day"] = 7
    _install(monkeypatch, shared=shared)

    job = _by_workload(
        get_workspace_schedule_policy("ws-gpu", session=object())  # type: ignore[arg-type]
    )["job"]

    assert job.max_runtime_minutes is None


def test_a_notebook_cap_is_a_wall_clock_shutdown_not_a_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = dict(_SHARED)
    shared["timed_shutdown"] = 1
    shared["shutdown_hour"] = 23
    shared["shutdown_minute"] = 5
    _install(monkeypatch, shared=shared)

    notebook = _by_workload(
        get_workspace_schedule_policy("ws-gpu", session=object())  # type: ignore[arg-type]
    )["notebook"]

    assert notebook.daily_shutdown == "23:05"
    assert notebook.max_runtime_minutes is None


def test_serving_reports_one_row_per_gpu_band(monkeypatch: pytest.MonkeyPatch) -> None:
    serving = {
        "enable_auto_stop": True,
        "items": [
            {
                "auto_stop_ruleset": (
                    '{"gate":"OR","conds":[{"gate":"OR","conds":'
                    '[{"hrs":5,"crit":"GPU","thresh":20}]}]}'
                ),
                "gpu_count_min": 8,
                "gpu_count_max": 16,
            },
            {
                "auto_stop_ruleset": (
                    '{"gate":"OR","conds":[{"gate":"OR","conds":'
                    '[{"hrs":1,"crit":"GPU","thresh":50}]}]}'
                ),
                "gpu_count_min": 1,
                "gpu_count_max": 4,
            },
        ],
    }
    _install(monkeypatch, serving=serving)

    rows = [
        policy
        for policy in get_workspace_schedule_policy("ws-gpu", session=object())  # type: ignore[arg-type]
        if policy.workload == "serving"
    ]

    # Collapsing the bands would drop whichever one the caller's deployment
    # actually falls into.
    assert [row.applies_to for row in rows] == ["8-16 GPU", "1-4 GPU"]
    assert rows[1].reclaim_description == "gpu < 50% for 1h (1-4 GPU)"


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


class _FakeSession:
    storage_state: dict[str, Any] = {}
    workspace_id = "ws-gpu"
    all_workspace_names = {"ws-gpu": "分布式训练空间", "ws-cpu": "CPU资源空间"}
    all_workspace_ids = ["ws-gpu", "ws-cpu"]


_POLICIES = [
    WorkloadSchedulePolicy(
        workload="notebook",
        auto_reclaim=True,
        reclaim_rule=schedule_config.ReclaimRule(
            gate="or",
            groups=(
                ("or", (schedule_config.ReclaimCondition("gpu", 3.0, 15.0),)),
                ("or", (schedule_config.ReclaimCondition("runtime", 18.0),)),
            ),
        ),
        auto_save=True,
    ),
    WorkloadSchedulePolicy(workload="job", auto_reclaim=False),
    WorkloadSchedulePolicy(workload="hpc", configured=False),
    WorkloadSchedulePolicy(workload="ray", auto_reclaim=False, max_runtime_minutes=14400),
    WorkloadSchedulePolicy(
        workload="serving",
        auto_reclaim=True,
        reclaim_rule=schedule_config.ReclaimRule(
            gate="or",
            groups=(("or", (schedule_config.ReclaimCondition("gpu", 5.0, 20.0),)),),
        ),
        applies_to="8-16 GPU",
    ),
]


def _patch_cli(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    config = config_module.Config(username="user", password="pass")
    calls: list[str] = []

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(policy_module, "get_web_session", lambda: _FakeSession())

    def _fake(workspace_id, *, session):
        calls.append(workspace_id)
        return _POLICIES

    monkeypatch.setattr(policy_module, "get_workspace_schedule_policy", _fake)
    return calls


def test_policy_table_names_the_rule_and_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch)

    result = CliRunner().invoke(
        cli_main, ["resources", "policy", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0, result.output
    assert "Idle Rule" in result.output
    assert "gpu < 15% for 3h OR runtime > 18h" in result.output
    assert "max 10d" in result.output
    assert "gpu < 20% for 5h (8-16 GPU)" in result.output


def test_an_undeclared_policy_reads_as_unknown_not_as_unlimited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli(monkeypatch)

    result = CliRunner().invoke(
        cli_main, ["resources", "policy", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0, result.output
    hpc_row = next(line for line in result.output.splitlines() if line.startswith("hpc "))
    # No "off" and no "none" on that row — both would be claims we cannot make.
    assert "off" not in hpc_row
    assert "none" not in hpc_row
    assert "declares no scheduling policy" in result.output


def test_policy_json_is_name_only_and_drops_maintainer_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli(monkeypatch)

    result = CliRunner().invoke(
        cli_main, ["--json", "resources", "policy", "--workspace", "分布式训练空间"]
    )

    assert result.exit_code == 0, result.output
    items = json.loads(result.output)["data"]["items"]
    assert [item["workload"] for item in items] == [
        "notebook",
        "job",
        "hpc",
        "ray",
        "serving",
    ]
    assert items[0]["workspace"] == "分布式训练空间"
    assert items[0]["auto_save"] is True
    assert items[2] == {
        "workspace": "分布式训练空间",
        "workload": "hpc",
        "configured": False,
    }
    assert items[3]["max_runtime"] == "10d"

    # Workspace capability switches, spec menus and platform handles are
    # maintainer facts, not user-actionable policy.
    for forbidden in ("train_enable", "config_id", "quota_id", "ws-", "left_time"):
        assert forbidden not in result.output


def test_policy_can_be_narrowed_to_one_workload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "resources", "policy", "--workspace", "分布式训练空间", "--workload", "hpc"],
    )

    assert result.exit_code == 0, result.output
    items = json.loads(result.output)["data"]["items"]
    assert [item["workload"] for item in items] == ["hpc"]


def test_policy_workspace_all_fans_out_and_labels_each_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_cli(monkeypatch)

    result = CliRunner().invoke(
        cli_main, ["--json", "resources", "policy", "--workspace", "all", "--all"]
    )

    assert result.exit_code == 0, result.output
    assert calls == ["ws-gpu", "ws-cpu"]
    items = json.loads(result.output)["data"]["items"]
    assert {item["workspace"] for item in items} == {"分布式训练空间", "CPU资源空间"}
    assert len(items) == 10


def test_policy_limit_and_all_conflict_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        policy_module,
        "get_workspace_schedule_policy",
        lambda *_args, **_kwargs: pytest.fail("budget conflict must fail first"),
    )

    result = CliRunner().invoke(
        cli_main,
        ["resources", "policy", "--workspace", "分布式训练空间", "--limit", "5", "--all"],
    )

    assert result.exit_code != 0


def test_policy_workloads_match_the_rest_of_the_cli_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inspire.cli.utils.resource_index import QUOTA_WORKLOADS

    _install(monkeypatch)
    policies = get_workspace_schedule_policy("ws-gpu", session=object())  # type: ignore[arg-type]

    # `--workload` offers these names and the rows are keyed by them, so a
    # wrapper that invented its own spelling would filter out every row.
    assert {policy.workload for policy in policies} == set(QUOTA_WORKLOADS)


def test_policy_workspace_metavar_accepts_all() -> None:
    option = {
        parameter.name: parameter for parameter in policy_module.policy_resources.params
    }["workspace"]

    assert option.metavar == "NAME|all"
