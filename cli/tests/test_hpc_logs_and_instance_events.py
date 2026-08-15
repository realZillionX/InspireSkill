"""`inspire hpc logs` and the instance-level side of `inspire hpc events`.

Both commands address instances by the Role / Rank identity that
`inspire hpc instances` prints, because the namespaced pod name the platform
wants is a handle that never crosses the output boundary. These tests pin that
translation, the log output budget, and the duplicate collapsing that makes a
`--tail` window worth reading.
"""

from __future__ import annotations

import importlib
import json
import time
from typing import Any

import pytest
from click.testing import CliRunner

from inspire.cli.commands.hpc.hpc_commands import (
    HPCInstanceSelectionError,
    hpc_instance_views,
    select_hpc_instance_views,
)
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api.hpc_jobs import HPC_LOG_MAX_WINDOW_MS

# The command objects shadow their own modules as package attributes, the same
# way `hpc_metrics` does, so reach for the modules by path.
hpc_events_mod = importlib.import_module("inspire.cli.commands.hpc.hpc_events")
hpc_logs_mod = importlib.import_module("inspire.cli.commands.hpc.hpc_logs")

_JOB_ID = "hpc-job-4a3737c4-2a30-4aa4-87e1-195edbe8fb6b"
_NS = "exploration-topic"

_INSTANCES = [
    {
        "component": "launcher",
        "name": f"{_NS}/hpc-job-136201-cluster-launcher-brcx5",
        "created_at": "1773388849000",
        "finished_at": "1773389205000",
        "status": "Deleted",
    },
    {
        "component": "slurmctld",
        "name": f"{_NS}/hpc-job-136201-cluster-slurmctld-0",
        "created_at": "1773388849000",
        "finished_at": "1773389205000",
        "status": "Deleted",
    },
    {
        "component": "slurmd",
        "name": f"{_NS}/hpc-job-136201-cluster-slurmd-0",
        "created_at": "1773388849000",
        "finished_at": "1773389205000",
        "status": "Deleted",
    },
]


def _log(message: str, *, pod: str = "hpc-job-136201-cluster-slurmd-0", nanos: str) -> dict:
    return {
        "log_id": "8878b003-0af1-4f8c-9e20-07318b29ecda",
        "message": message,
        "node": "",
        "pod_name": pod,
        "time": "",
        "timestamp_ms": "1781499389644",
        "timestamp_str": f"2026-06-15T04:56:29.{nanos}Z",
    }


def _patch_common(monkeypatch: pytest.MonkeyPatch, module) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        module.Config,
        "from_files_and_env",
        lambda **_kwargs: (object(), {}),
    )
    monkeypatch.setattr(module, "get_web_session", lambda: object())
    monkeypatch.setattr(
        module,
        "_run_readonly_hpc_operation",
        lambda _ctx, **kwargs: kwargs["operation"](_JOB_ID, kwargs["session"]),
    )


def _patch_logs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    logs: list[dict],
    total: int | None = None,
    instances: list[dict] | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    _patch_common(monkeypatch, hpc_logs_mod)
    monkeypatch.setattr(
        hpc_logs_mod,
        "_fetch_hpc_instances",
        lambda _job_id, **_kwargs: (
            list(_INSTANCES if instances is None else instances),
            len(_INSTANCES if instances is None else instances),
        ),
    )

    def _fake_logs(**kwargs):  # noqa: ANN003
        calls.append(kwargs)
        return list(logs), len(logs) if total is None else total

    monkeypatch.setattr(hpc_logs_mod, "list_hpc_job_logs", _fake_logs)
    return calls


# --- instance identity ------------------------------------------------------


def test_a_unique_role_is_its_own_label() -> None:
    views = hpc_instance_views(_INSTANCES)

    assert [view.label for view in views] == ["launcher", "slurmctld", "slurmd"]
    assert [view.pod for view in views] == [
        "hpc-job-136201-cluster-launcher-brcx5",
        "hpc-job-136201-cluster-slurmctld-0",
        "hpc-job-136201-cluster-slurmd-0",
    ]
    assert views[0].handle.startswith(f"{_NS}/")


def test_a_replicated_role_takes_the_rank_shown_by_hpc_instances() -> None:
    views = hpc_instance_views(
        [
            {"component": "slurmctld", "name": "ns/a"},
            {"component": "slurmd", "name": "ns/b"},
            {"component": "slurmd", "name": "ns/c"},
        ]
    )

    assert [view.label for view in views] == ["slurmctld", "slurmd-1", "slurmd-2"]


def test_selectors_match_the_role_or_the_ranked_label() -> None:
    views = hpc_instance_views(
        [
            {"component": "slurmd", "name": "ns/b"},
            {"component": "slurmd", "name": "ns/c"},
        ]
    )

    assert [view.handle for view in select_hpc_instance_views(views, ("slurmd",))] == [
        "ns/b",
        "ns/c",
    ]
    assert [view.handle for view in select_hpc_instance_views(views, ("SLURMD-1",))] == [
        "ns/c"
    ]


def test_an_unmatched_selector_raises_instead_of_emptying_the_scope() -> None:
    """An empty pod list would make the answer read as "nothing happened"."""
    with pytest.raises(HPCInstanceSelectionError, match="Available: launcher, slurmctld, slurmd"):
        select_hpc_instance_views(hpc_instance_views(_INSTANCES), ("worker",))


# --- hpc logs ---------------------------------------------------------------


def test_logs_send_every_namespaced_handle_and_label_the_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_logs(monkeypatch, logs=[_log("hello", nanos="100000000")])

    result = CliRunner().invoke(
        cli_main, ["hpc", "logs", "prep-a", "--workspace", "CPU Room"]
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["pod_names"] == [inst["name"] for inst in _INSTANCES]
    assert calls[0]["job_id"] == _JOB_ID
    assert "HPC Logs" in result.output
    assert "slurmd hello" in result.output
    # The pod handle is a platform id and must not reach the output at all.
    assert "hpc-job-136201" not in result.output
    assert "<redacted>" not in result.output


def test_logs_narrow_the_pod_list_to_the_selected_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_logs(monkeypatch, logs=[])

    result = CliRunner().invoke(
        cli_main,
        ["hpc", "logs", "prep-a", "--workspace", "CPU Room", "--instance", "slurmctld"],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["pod_names"] == [f"{_NS}/hpc-job-136201-cluster-slurmctld-0"]
    assert "No HPC logs found." in result.output


def test_logs_reject_an_unknown_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_logs(monkeypatch, logs=[])

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "hpc",
            "logs",
            "prep-a",
            "--workspace",
            "CPU Room",
            "--instance",
            "worker",
        ],
    )

    assert result.exit_code == 12
    payload = json.loads(result.output)
    assert payload["error"]["type"] == "ValidationError"
    assert "Available: launcher, slurmctld, slurmd" in payload["error"]["message"]


def test_logs_report_the_truncation_budget_in_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_logs(
        monkeypatch,
        logs=[_log(f"line {index}", nanos=f"{index:09d}") for index in range(5)],
        total=134,
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "hpc", "logs", "prep-a", "--workspace", "CPU Room", "--tail", "2"],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["shown"] == 2
    assert data["total"] == 134
    assert data["truncated"] is True
    assert data["limit"] == 2
    assert data["character_limit"] == 16000
    assert [item["message"] for item in data["logs"]] == ["line 3", "line 4"]
    assert all(item["pod_name"] == "slurmd" for item in data["logs"])
    assert all("log_id" not in item for item in data["logs"])


def test_logs_order_a_sub_millisecond_burst_by_the_precise_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every record shares one `timestamp_ms`; only nanoseconds separate them."""
    _patch_logs(
        monkeypatch,
        logs=[
            _log("third", nanos="644467658"),
            _log("first", nanos="644327598"),
            _log("second", nanos="644384600"),
        ],
    )

    result = CliRunner().invoke(
        cli_main, ["hpc", "logs", "prep-a", "--workspace", "CPU Room"]
    )

    assert result.exit_code == 0, result.output
    messages = [line.split()[-1] for line in result.output.splitlines()[1:]]
    assert messages == ["first", "second", "third"]


@pytest.mark.parametrize(
    ("extra", "expected_page_sizes"),
    (
        (["--all"], [100, 134]),
        ([], [100, 134]),
        (["--tail", "5"], [100, 134]),
        (["--head", "5"], [100]),
    ),
)
def test_reading_from_the_end_pulls_the_whole_window_first(
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
    expected_page_sizes: list[int],
) -> None:
    """`page_size` drops the newest records, so a tail cannot be asked for."""
    calls = _patch_logs(
        monkeypatch,
        logs=[_log("only", nanos="000000001")],
        total=134,
    )

    result = CliRunner().invoke(
        cli_main, ["hpc", "logs", "prep-a", "--workspace", "CPU Room", *extra]
    )

    assert result.exit_code == 0, result.output
    assert [call["page_size"] for call in calls] == expected_page_sizes


def test_a_job_without_instances_is_not_an_empty_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_logs(monkeypatch, logs=[], instances=[])

    result = CliRunner().invoke(
        cli_main, ["--json", "hpc", "logs", "prep-a", "--workspace", "CPU Room"]
    )

    assert result.exit_code != 0
    assert json.loads(result.output)["error"]["type"] == "LogNotFound"


@pytest.mark.parametrize(
    ("extra", "expected"),
    (
        (["--tail", "5", "--head", "5"], "--tail and --head cannot be used together."),
        (["--all", "--tail", "5"], "--all cannot be combined with --tail."),
        (["--window", "nope"], "use a window like 30m or 2h"),
    ),
)
def test_logs_reject_contradictory_windows_and_budgets(
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
    expected: str,
) -> None:
    _patch_logs(monkeypatch, logs=[])

    result = CliRunner().invoke(
        cli_main, ["hpc", "logs", "prep-a", "--workspace", "CPU Room", *extra]
    )

    assert result.exit_code == 12
    assert expected in result.output


def test_the_window_is_clamped_below_the_platform_month_cap() -> None:
    """A wider window answers `InternalError`, which costs three retries first."""
    start, end, clamped = hpc_logs_mod._log_time_range(
        [{"created_at": "1000000000000", "finished_at": "1773389205000"}],
        None,
    )

    assert clamped is True
    assert end - start == HPC_LOG_MAX_WINDOW_MS

    start, end, clamped = hpc_logs_mod._log_time_range(
        [{"created_at": "1773388849000", "finished_at": "1773389205000"}],
        None,
    )

    assert clamped is False
    assert start < 1773388849000 < end


def test_a_running_instance_extends_the_window_to_now() -> None:
    """An empty `finished_at` means "still going", not "end time unknown"."""
    now_ms = int(time.time() * 1000)
    created = now_ms - 60 * 60 * 1000
    stopped = now_ms - 30 * 60 * 1000

    start, end, clamped = hpc_logs_mod._log_time_range(
        [
            {"created_at": str(created), "finished_at": str(stopped)},
            {"created_at": str(created), "finished_at": ""},
        ],
        None,
    )

    assert clamped is False
    assert start < created
    assert end >= now_ms

    _start, finished_end, _clamped = hpc_logs_mod._log_time_range(
        [{"created_at": str(created), "finished_at": str(stopped)}],
        None,
    )

    assert finished_end < now_ms


# --- hpc events --instance --------------------------------------------------


def _pod_event(reason: str, last: str, *, message: str | None = None) -> dict:
    return {
        "reason": reason,
        "message": message or f"{reason} happened",
        "from": "kubelet",
        "first_timestamp": "1773388870000",
        "last_timestamp": last,
        "object_id": f"{_NS}/hpc-job-136201-cluster-slurmd-0",
        "object_type": "HPC_JOB_INSTANCE",
    }


def _patch_events(
    monkeypatch: pytest.MonkeyPatch,
    *,
    instance_events: list[dict],
    job_events: list[dict] | None = None,
) -> list[list[str]]:
    seen: list[list[str]] = []
    _patch_common(monkeypatch, hpc_events_mod)
    monkeypatch.setattr(
        hpc_events_mod,
        "_fetch_hpc_instances",
        lambda _job_id, **_kwargs: (list(_INSTANCES), len(_INSTANCES)),
    )
    monkeypatch.setattr(
        hpc_events_mod,
        "list_hpc_instance_events",
        lambda handles, _session, **_kwargs: (
            seen.append(list(handles)) or list(instance_events)
        ),
    )
    monkeypatch.setattr(
        hpc_events_mod,
        "list_hpc_job_events",
        lambda _job_id, session=None: list(job_events or []),
    )
    return seen


def test_the_default_merges_controller_and_pod_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two views are disjoint sets, so reading one answers half the question."""
    seen = _patch_events(
        monkeypatch,
        instance_events=[_pod_event("BackOff", "2")],
        job_events=[_pod_event("CreatedSlurmCluster", "1")],
    )

    result = CliRunner().invoke(
        cli_main, ["--json", "hpc", "events", "prep-a", "--workspace", "CPU Room"]
    )

    assert result.exit_code == 0, result.output
    assert seen == [[inst["name"] for inst in _INSTANCES]]
    items = json.loads(result.output)["data"]["items"]
    assert [item["reason"] for item in items] == ["CreatedSlurmCluster", "BackOff"]


def test_an_instance_selector_switches_to_the_pod_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_events(monkeypatch, instance_events=[_pod_event("Scheduled", "1")])

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "hpc",
            "events",
            "prep-a",
            "--workspace",
            "CPU Room",
            "--instance",
            "slurmd",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == [[f"{_NS}/hpc-job-136201-cluster-slurmd-0"]]
    assert json.loads(result.output)["data"]["items"][0]["reason"] == "Scheduled"


def test_an_instance_selector_narrows_the_pod_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Narrowing drops the controller view with it — one role, one question."""
    seen = _patch_events(
        monkeypatch,
        instance_events=[_pod_event("BackOff", "2")],
        job_events=[_pod_event("CreatedSlurmCluster", "1")],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "hpc",
            "events",
            "prep-a",
            "--workspace",
            "CPU Room",
            "--instance",
            "slurmd",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == [[f"{_NS}/hpc-job-136201-cluster-slurmd-0"]]
    items = json.loads(result.output)["data"]["items"]
    assert [item["reason"] for item in items] == ["BackOff"]


def test_repeated_occurrences_collapse_into_the_count_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The platform repeats a row per occurrence and never fills `count`."""
    _patch_events(
        monkeypatch,
        instance_events=[
            _pod_event("BackOff", "3"),
            _pod_event("BackOff", "3"),
            _pod_event("BackOff", "3"),
            _pod_event("Scheduled", "1"),
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "hpc",
            "events",
            "prep-a",
            "--workspace",
            "CPU Room",
        ],
    )

    assert result.exit_code == 0, result.output
    items = json.loads(result.output)["data"]["items"]
    assert [(item["reason"], item.get("count")) for item in items] == [
        ("Scheduled", None),
        ("BackOff", 3),
    ]


def test_instance_events_are_ordered_so_tail_means_recent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ListSlurmdPodEvent` takes no sorter and several pods are concatenated."""
    _patch_events(
        monkeypatch,
        instance_events=[
            _pod_event("BackOff", "1773389457000"),
            _pod_event("Scheduled", "1773388851000"),
            _pod_event("Pulled", "1773388867000"),
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "hpc",
            "events",
            "prep-a",
            "--workspace",
            "CPU Room",
            "--tail",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    items = json.loads(result.output)["data"]["items"]
    assert [item["reason"] for item in items] == ["Pulled", "BackOff"]


def test_pod_events_name_the_instance_they_came_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merged per-pod window is unreadable if no row says which pod it is."""
    _patch_events(
        monkeypatch,
        instance_events=[
            _pod_event("Scheduled", "1"),
            {
                **_pod_event("BackOff", "2"),
                "object_id": f"{_NS}/hpc-job-136201-cluster-slurmctld-0",
            },
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "hpc",
            "events",
            "prep-a",
            "--workspace",
            "CPU Room",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Instance" in result.output
    assert "slurmd" in result.output
    assert "slurmctld" in result.output
    assert "hpc-job-136201-cluster-slurmd-0" not in result.output


def test_job_level_events_keep_the_narrow_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Job-level rows have no instance identity; the column would be dashes."""
    _patch_events(
        monkeypatch,
        instance_events=[],
        job_events=[_pod_event("CreatedSlurmCluster", "1")],
    )

    result = CliRunner().invoke(
        cli_main, ["hpc", "events", "prep-a", "--workspace", "CPU Room"]
    )

    assert result.exit_code == 0, result.output
    assert "Instance" not in result.output


def test_events_reject_an_unknown_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_events(monkeypatch, instance_events=[])

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "hpc",
            "events",
            "prep-a",
            "--workspace",
            "CPU Room",
            "--instance",
            "worker",
        ],
    )

    assert result.exit_code == 12
    assert json.loads(result.output)["error"]["type"] == "ValidationError"
