from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api import MemberUsage, TaskUsage
from inspire.platform.web.browser_api.availability import api
from inspire.platform.web.session import TransientAPIError

_WS = "ws-00000000-0000-0000-0000-0000000000aa"


class _Session:
    workspace_id = _WS
    all_workspace_ids = [_WS]
    all_workspace_names = {_WS: "Default WS"}


def _patch_config(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config_module.Config(
        username="user",
        password="pass",
        base_url="https://qz.sii.edu.cn",
    )
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **kwargs: (cfg, {})),
    )


def _task(
    *,
    name: str,
    user: str,
    project: str,
    gpus: int,
    nodes: tuple[str, ...],
    gpu_usage_rate: float = 0.5,
    cpus: float = 0.0,
    priority: int = 4,
) -> TaskUsage:
    return TaskUsage(
        task_id=f"job-{name}",
        name=name,
        task_type="distributed_training",
        status="RUNNING",
        user_name=user,
        project_name=project,
        gpus=gpus,
        cpus=cpus or gpus * 20.0,
        memory_gib=gpus * 100.0,
        gpu_usage_rate=gpu_usage_rate,
        cpu_usage_rate=0.1,
        node_names=nodes,
        created_at="2026-08-15 10:00:00 +0800 CST",
        running_time_ms=1000,
        priority=priority,
    )


# --- wrapper: request shape and paging -------------------------------------


def test_task_dimension_scopes_workspace_inside_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-level `workspace_id` is rejected as an unknown field by the gateway."""
    seen: list[dict] = []

    def _fake(_session, _method, _path, *, referer, body, timeout):
        seen.append(body)
        return {"ResponseMetadata": {}, "Result": {"task_dimensions": [], "total": 0}}

    monkeypatch.setattr(api, "_request_json", _fake)

    api.list_task_usage("ws-1", session=object())  # type: ignore[arg-type]

    assert seen[0]["filter"] == {"workspace_id": "ws-1"}
    assert "workspace_id" not in {key for key in seen[0] if key != "filter"}
    assert seen[0]["page_size"] == api._DIMENSION_PAGE_SIZE == 5000


def test_task_dimension_can_scope_user_and_project_inside_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []

    def _fake(_session, _method, _path, *, referer, body, timeout):
        seen.append(body)
        return {"Result": {"task_dimensions": [], "total": 0}}

    monkeypatch.setattr(api, "_request_json", _fake)

    api.list_task_usage(
        "ws-1",
        user_id="user-1",
        project_id="project-1",
        session=object(),  # type: ignore[arg-type]
    )

    assert seen[0]["filter"] == {
        "workspace_id": "ws-1",
        "user_id": "user-1",
        "project_id": "project-1",
    }


def test_task_dimension_pages_against_total_not_page_size_minus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`page_size: -1` answers 10 rows here, so paging must follow `total`."""
    pages: list[int] = []

    def _fake(_session, _method, _path, *, referer, body, timeout):
        page = int(body["PageNumber"])
        pages.append(page)
        assert body["page_size"] > 0, "the -1 shortcut silently truncates this Action"
        start = (page - 1) * 500
        rows = [
            {"id": f"job-{index}", "name": f"t{index}", "gpu": {"total": 1}}
            for index in range(start, min(start + 500, 1200))
        ]
        return {
            "ResponseMetadata": {},
            "Result": {"task_dimensions": rows, "total": 1200},
        }

    monkeypatch.setattr(api, "_request_json", _fake)

    rows = api._list_dimension_rows(
        "ListTaskDimension",
        "task_dimensions",
        workspace_id="ws-1",
        session=object(),  # type: ignore[arg-type]
        page_size=500,
    )

    assert pages == [1, 2, 3]
    assert len(rows) == 1200


def test_task_dimension_keeps_paging_when_server_returns_short_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages: list[int] = []

    def _fake(_session, _method, _path, *, referer, body, timeout):
        page = int(body["PageNumber"])
        pages.append(page)
        start = (page - 1) * 2
        rows = [
            {"id": f"job-{index}", "gpu": {"total": 1}}
            for index in range(start, min(start + 2, 4))
        ]
        return {"Result": {"task_dimensions": rows, "total": 4}}

    monkeypatch.setattr(api, "_request_json", _fake)

    rows = api._list_dimension_rows(
        "ListTaskDimension",
        "task_dimensions",
        workspace_id="ws-1",
        session=object(),  # type: ignore[arg-type]
        page_size=500,
    )

    assert pages == [1, 2]
    assert len(rows) == 4


def test_task_dimension_refuses_to_return_a_safety_cap_partial_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "_DIMENSION_PAGE_CAP", 1)
    monkeypatch.setattr(
        api,
        "_request_json",
        lambda *_args, **_kwargs: {
            "Result": {
                "task_dimensions": [{"id": "job-1"}],
                "total": 2,
            }
        },
    )

    with pytest.raises(ValueError, match="safe pagination limit"):
        api._list_dimension_rows(
            "ListTaskDimension",
            "task_dimensions",
            workspace_id="ws-1",
            session=object(),  # type: ignore[arg-type]
            page_size=1,
        )


def test_task_dimension_drops_rows_repeated_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The list churns while it is paged; a repeat must not inflate totals."""

    def _fake(_session, _method, _path, *, referer, body, timeout):
        page = int(body["PageNumber"])
        rows = (
            [{"id": f"job-{index}", "gpu": {"total": 1}} for index in range(500)]
            if page == 1
            else [{"id": "job-0", "gpu": {"total": 1}}, {"id": "job-x", "gpu": {"total": 1}}]
        )
        return {
            "ResponseMetadata": {},
            "Result": {"task_dimensions": rows, "total": 502},
        }

    monkeypatch.setattr(api, "_request_json", _fake)

    rows = api._list_dimension_rows(
        "ListTaskDimension",
        "task_dimensions",
        workspace_id="ws-1",
        session=object(),  # type: ignore[arg-type]
        page_size=500,
    )

    assert len(rows) == 501


def test_task_dimension_reads_string_total(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def _fake(_session, _method, _path, *, referer, body, timeout):
        calls.append(int(body["PageNumber"]))
        return {
            "ResponseMetadata": {},
            "Result": {
                "task_dimensions": [{"id": "job-1", "gpu": {"total": 8}}],
                "total": "1",
            },
        }

    monkeypatch.setattr(api, "_request_json", _fake)

    usages = api.list_task_usage("ws-1", session=object())  # type: ignore[arg-type]

    assert calls == [1]
    assert usages[0].gpus == 8


def test_task_dimension_projects_nested_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        api,
        "_request_json",
        lambda *_a, **_k: {
            "ResponseMetadata": {},
            "Result": {
                "task_dimensions": [
                    {
                        "id": "job-1",
                        "name": "pretrain",
                        "type": "distributed_training",
                        "status": "RUNNING",
                        "user": {"id": "user-1", "name": "Ada"},
                        "project": {"id": "project-1", "name": "Vision"},
                        "cpu": {"total": 160, "usage_rate": 0.25},
                        "gpu": {"total": 8, "usage_rate": 0.9, "used": 0},
                        "memory": {"total": 1600},
                        "nodes_occupied": {"count": 1, "nodes": ["gpu-1"]},
                        "created_at": "2026-08-15 10:00:00 +0800 CST",
                        "running_time_ms": "1234",
                        "priority": 1,
                    }
                ],
                "total": 1,
            },
        },
    )

    usage = api.list_task_usage("ws-1", session=object())[0]  # type: ignore[arg-type]

    assert usage.user_name == "Ada"
    assert usage.project_name == "Vision"
    assert usage.gpus == 8
    # `gpu.used` is always 0 on live rows; utilisation only exists as a rate.
    assert usage.gpu_usage_rate == 0.9
    assert usage.node_names == ("gpu-1",)
    assert usage.running_time_ms == 1234
    # The submitted priority, which is what separates the cards a
    # higher-priority job could take from the ones it could not.
    assert usage.priority == 1


def test_task_dimension_never_folds_a_transient_failure_into_no_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args, **_kwargs):
        raise TransientAPIError("throttled")

    monkeypatch.setattr(api, "_request_json", _boom)

    with pytest.raises(TransientAPIError):
        api.list_task_usage("ws-1", session=object())  # type: ignore[arg-type]


def test_member_usage_reads_the_per_kind_node_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[dict] = []

    def _request(*_args, **kwargs):
        bodies.append(kwargs["body"])
        return {
            "ResponseMetadata": {},
            "Result": {
                "user_dimensions": [
                    {
                        "user": {"name": "Ada"},
                        "project": {"project_name": "Vision"},
                        "cpu": {"total": 15},
                        "gpu": {"total": 0},
                        "memory": {"total": 60},
                        "cpu_nodes_occupied": {"count": 4, "nodes": ["c1"]},
                        "gpu_nodes_occupied": {"count": 0, "nodes": []},
                        "hpc_nodes_occupied": {"count": 1, "nodes": ["h1"]},
                    }
                ],
                "total": 1,
            },
        }

    monkeypatch.setattr(api, "_request_json", _request)

    session = SimpleNamespace(user_detail={"id": "user-1"})
    usage = api.list_member_usage("ws-1", session=session)[0]  # type: ignore[arg-type]

    assert usage == MemberUsage(
        user_name="Ada",
        project_name="Vision",
        gpus=0,
        cpus=15.0,
        memory_gib=60.0,
        gpu_nodes=0,
        cpu_nodes=4,
        hpc_nodes=1,
    )
    assert bodies == [
        {
            "filter": {"workspace_id": "ws-1", "user_id": "user-1"},
            "PageNumber": 1,
            "page_size": api._DIMENSION_PAGE_SIZE,
        }
    ]


def test_member_usage_refuses_to_call_an_unfiltered_workspace_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inspire.platform.web.browser_api import jobs

    monkeypatch.setattr(jobs, "get_current_user", lambda _session: {})
    with pytest.raises(ValueError, match="Could not resolve the current user"):
        api.list_member_usage(
            "ws-1",
            session=SimpleNamespace(user_detail={}),  # type: ignore[arg-type]
        )


# --- command ---------------------------------------------------------------


def test_usage_removes_legacy_by_option() -> None:
    runner = CliRunner()
    help_result = runner.invoke(cli_main, ["resources", "usage", "--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "--by" not in help_result.output

    result = runner.invoke(
        cli_main,
        ["resources", "usage", "--workspace", "Default WS", "--by", "user"],
    )
    assert result.exit_code != 0
    assert "No such option '--by'" in result.output


def _patch_command(monkeypatch: pytest.MonkeyPatch, tasks: list[TaskUsage]) -> None:
    from inspire.cli.commands.resources import resources_usage as usage_module

    _patch_config(monkeypatch)
    monkeypatch.setattr(usage_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        usage_module,
        "workspace_name_map",
        lambda _session: {_WS: "Default WS"},
    )
    monkeypatch.setattr(
        usage_module.browser_api_module,
        "list_task_usage",
        lambda _workspace_id, **_kwargs: tasks,
    )


def test_usage_weights_gpu_busy_by_cards_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_command(
        monkeypatch,
        [
            _task(
                name="big",
                user="Ada",
                project="Vision",
                gpus=8,
                nodes=("n1",),
                gpu_usage_rate=1.0,
            ),
            _task(
                name="small",
                user="Ada",
                project="Vision",
                gpus=2,
                nodes=("n2",),
                gpu_usage_rate=0.0,
            ),
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "resources",
            "usage",
            "--workspace",
            "Default WS",
        ],
    )

    assert result.exit_code == 0, result.output
    row = json.loads(result.output)["data"]["items"][0]
    assert row["gpu_usage_rate"] == 0.8


def test_usage_defaults_to_project_user_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_command(
        monkeypatch,
        [
            _task(name="vision-a", user="Ada", project="Vision", gpus=4, nodes=("n1",)),
            _task(name="vision-b", user="Bo", project="Vision", gpus=8, nodes=("n2",)),
            _task(name="speech-a", user="Ada", project="Speech", gpus=2, nodes=("n3",)),
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "resources", "usage", "--workspace", "Default WS"],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["scope"] == "project-user"
    rows = {(row["project"], row["user"]): row for row in data["items"]}
    assert rows[("Vision", "Ada")]["gpus"] == 4
    assert rows[("Vision", "Bo")]["gpus"] == 8
    assert rows[("Speech", "Ada")]["gpus"] == 2


def test_usage_project_filter_and_details_share_the_same_task_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_command(
        monkeypatch,
        [
            _task(name="vision-a", user="Ada", project="Vision", gpus=4, nodes=("n1",)),
            _task(name="speech-a", user="Ada", project="Speech", gpus=8, nodes=("n2",)),
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "resources",
            "usage",
            "--workspace",
            "Default WS",
            "--project",
            "Vision",
            "--details",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["scope"] == "task"
    assert data["filters"] == {"project": "Vision"}
    assert [row["task"] for row in data["items"]] == ["vision-a"]


def test_usage_details_sorts_by_capacity_held(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_command(
        monkeypatch,
        [
            _task(name="small", user="Ada", project="Vision", gpus=1, nodes=("n1",)),
            _task(name="huge", user="Bo", project="Vision", gpus=64, nodes=("n2",)),
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "resources", "usage", "--workspace", "Default WS", "--details"],
    )

    assert result.exit_code == 0, result.output
    items = json.loads(result.output)["data"]["items"]
    assert [row["task"] for row in items] == ["huge", "small"]


@pytest.mark.parametrize("mode", ([], ["--details"], ["--mine"]))
def test_usage_refuses_a_workspace_fanout(
    monkeypatch: pytest.MonkeyPatch, mode: list[str]
) -> None:
    """`all` is refused in every mode, before any workspace is swept.

    The rollups bucket per workspace, so a fanout would emit one row per
    workspace-and-user pair under a shared ranking — a platform-wide leader
    board that the data cannot support. Quota and scheduling are per workspace
    anyway, so there is no decision on the other side of the fanout.
    """
    from inspire.cli.commands.resources import resources_usage as usage_module

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("--workspace all must be refused before any sweep")

    _patch_command(monkeypatch, [])
    monkeypatch.setattr(usage_module.browser_api_module, "list_task_usage", _refuse)
    monkeypatch.setattr(usage_module.browser_api_module, "list_member_usage", _refuse)

    result = CliRunner().invoke(
        cli_main,
        ["resources", "usage", "--workspace", "all", *mode],
    )

    assert result.exit_code != 0
    assert "--workspace requires one workspace name for this command." in result.output


def test_usage_defaults_to_twenty_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_command(
        monkeypatch,
        [
            _task(
                name=f"t{index}",
                user=f"User {index:02d}",
                project="Vision",
                gpus=index + 1,
                nodes=(f"n{index}",),
            )
            for index in range(25)
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "resources", "usage", "--workspace", "Default WS"],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert len(data["items"]) == 20
    assert data["shown"] == 20
    assert data["total"] == 25
    assert data["truncated"] is True


def test_usage_mine_uses_the_single_request_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inspire.cli.commands.resources import resources_usage as usage_module

    _patch_command(monkeypatch, [])

    def _reject(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("--mine must not sweep the whole workspace")

    monkeypatch.setattr(usage_module.browser_api_module, "list_task_usage", _reject)
    monkeypatch.setattr(
        usage_module.browser_api_module,
        "list_member_usage",
        lambda _workspace_id, **_kwargs: [
            MemberUsage(
                user_name="Ada",
                project_name="Vision",
                gpus=8,
                cpus=160.0,
                memory_gib=1600.0,
                gpu_nodes=1,
                cpu_nodes=0,
                hpc_nodes=0,
            )
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "resources", "usage", "--workspace", "Default WS", "--mine"],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["scope"] == "mine"
    assert data["items"][0]["project"] == "Vision"
    assert data["items"][0]["gpu_nodes"] == 1


def _patch_fair(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    """Pin the workspace priority contract; an exception means "unreadable"."""
    from inspire.cli.commands.resources import resources_usage as usage_module

    def _answer(_session, _workspace_id):  # noqa: ANN001
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(usage_module, "is_fair_scheduling_workspace", _answer)


def test_usage_counts_reclaimable_gpus_by_the_workspace_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A holder is the holder; only the low-priority part can be taken."""
    tasks = [
        _task(name="lo", user="Ada", project="V", gpus=8, nodes=("n1",), priority=1),
        _task(name="hi", user="Ada", project="V", gpus=16, nodes=("n2",), priority=4),
        _task(name="legacy", user="Ada", project="V", gpus=4, nodes=("n3",), priority=6),
    ]
    _patch_command(monkeypatch, tasks)
    _patch_fair(monkeypatch, True)

    data = json.loads(
        CliRunner()
        .invoke(cli_main, ["--json", "resources", "usage", "--workspace", "Default WS"])
        .output
    )["data"]
    row = data["items"][0]
    assert row["gpus"] == 28
    # Fair scheduling: everything under 4 is LOW, 4 and above is not.
    assert row["low_priority_gpus"] == 8

    # The same holdings in a 1..10 workspace, where the low band reaches 3.
    _patch_fair(monkeypatch, False)
    data = json.loads(
        CliRunner()
        .invoke(cli_main, ["--json", "resources", "usage", "--workspace", "Default WS"])
        .output
    )["data"]
    assert data["items"][0]["low_priority_gpus"] == 8


def test_usage_reports_an_unreadable_contract_as_unknown_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero reclaimable and "cannot tell" drive opposite decisions."""
    _patch_command(
        monkeypatch,
        [_task(name="a", user="Ada", project="V", gpus=8, nodes=("n1",), priority=1)],
    )
    _patch_fair(monkeypatch, RuntimeError("policy unavailable"))

    result = CliRunner().invoke(
        cli_main, ["--json", "resources", "usage", "--workspace", "Default WS"]
    )

    assert result.exit_code == 0, result.output
    row = json.loads(result.output)["data"]["items"][0]
    assert row["gpus"] == 8
    assert row["low_priority_gpus"] is None


def test_usage_never_reads_a_missing_priority_as_preemptible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`priority: 0` is "the row did not say", and inventing takeable capacity
    from silence is the one error that sends someone to argue for cards that
    were never available."""
    _patch_command(
        monkeypatch,
        [_task(name="a", user="Ada", project="V", gpus=8, nodes=("n1",), priority=0)],
    )
    _patch_fair(monkeypatch, True)

    data = json.loads(
        CliRunner()
        .invoke(cli_main, ["--json", "resources", "usage", "--workspace", "Default WS"])
        .output
    )["data"]
    assert data["items"][0]["low_priority_gpus"] == 0


def _patch_groups(monkeypatch: pytest.MonkeyPatch, groups: list[dict]) -> None:
    from inspire.cli.commands.resources import resources_usage as usage_module

    monkeypatch.setattr(
        usage_module.browser_api_module,
        "list_compute_groups",
        lambda **_kwargs: groups,
        raising=False,
    )


_GROUPS = [
    {"logic_compute_group_id": "lcg-a", "name": "训练区-H200-1号机房"},
    {"logic_compute_group_id": "lcg-b", "name": "训练区-H200-3号机房"},
    {"logic_compute_group_id": "lcg-c", "name": "开发区-H100-183核"},
]


def test_usage_group_asks_the_platform_per_matching_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keyword is a substring, and each match is scoped server-side."""
    from inspire.cli.commands.resources import resources_usage as usage_module

    _patch_command(monkeypatch, [])
    _patch_groups(monkeypatch, _GROUPS)
    asked: list[object] = []

    def _tasks(_workspace_id, *, logic_compute_group_id=None, **_kwargs):  # noqa: ANN001
        asked.append(logic_compute_group_id)
        return [
            _task(
                name=f"t-{logic_compute_group_id}",
                user="Ada",
                project="Vision",
                gpus=8,
                nodes=(f"n-{logic_compute_group_id}",),
            )
        ]

    monkeypatch.setattr(usage_module.browser_api_module, "list_task_usage", _tasks)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "resources", "usage", "--workspace", "Default WS", "--group", "H200"],
    )

    assert result.exit_code == 0, result.output
    assert asked == ["lcg-a", "lcg-b"]
    data = json.loads(result.output)["data"]
    assert data["compute_groups"] == ["训练区-H200-1号机房", "训练区-H200-3号机房"]
    # Both groups' tasks belong to Ada, so the rollup folds them into one row.
    assert data["items"][0]["gpus"] == 16


def test_usage_group_refuses_a_keyword_that_matches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silently answering for the whole workspace would read as an empty group."""
    from inspire.cli.commands.resources import resources_usage as usage_module

    _patch_command(
        monkeypatch,
        [_task(name="a", user="Ada", project="Vision", gpus=8, nodes=("n1",))],
    )
    _patch_groups(monkeypatch, _GROUPS)
    monkeypatch.setattr(
        usage_module.browser_api_module,
        "list_task_usage",
        lambda *_a, **_k: pytest.fail("must not sweep the workspace"),
    )

    result = CliRunner().invoke(
        cli_main,
        ["resources", "usage", "--workspace", "Default WS", "--group", "MI300"],
    )

    assert result.exit_code != 0
    assert "MI300" in result.output


def test_usage_rejects_group_with_mine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-project record `--mine` reads carries no compute group at all."""
    _patch_command(monkeypatch, [])
    _patch_groups(monkeypatch, _GROUPS)

    result = CliRunner().invoke(
        cli_main,
        ["resources", "usage", "--workspace", "Default WS", "--mine", "--group", "H200"],
    )

    assert result.exit_code != 0
    assert "--group" in result.output


def test_usage_keeps_platform_handles_out_of_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_project_id = "project-11111111-1111-1111-1111-111111111111"
    _patch_command(
        monkeypatch,
        [
            _task(
                name=f"train {raw_project_id}",
                user="Ada",
                project=f"Vision {raw_project_id}",
                gpus=8,
                nodes=("qb-prod-gpu542",),
            )
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["resources", "usage", "--workspace", "Default WS", "--details"],
    )

    assert result.exit_code == 0, result.output
    assert raw_project_id not in result.output
    assert "<redacted>" not in result.output
    # Node names are platform topology; only the count is a public fact.
    assert "qb-prod-gpu542" not in result.output
    assert "train" in result.output


def test_usage_reports_an_empty_workspace_without_claiming_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_command(monkeypatch, [])

    result = CliRunner().invoke(
        cli_main,
        ["resources", "usage", "--workspace", "Default WS"],
    )

    assert result.exit_code == 0, result.output
    assert "No live workloads" in result.output
