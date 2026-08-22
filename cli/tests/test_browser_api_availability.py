from __future__ import annotations

import json
import threading

import pytest

from inspire.platform.web.browser_api.availability import api

# One spec menu as the live platform sends it: a JSON-encoded *string*, with
# the three spellings of "no restriction" the platform mixes freely.
_TRAIN_SPEC_MENU = json.dumps(
    [
        {"id": "spec-any-null", "name": "8卡160核", "allowed_priority_levels": None},
        {"id": "spec-any-empty", "name": "8卡110核", "allowed_priority_levels": []},
        {"id": "spec-any-missing", "name": "4卡55核"},
        {"id": "spec-low", "name": "1卡20核", "allowed_priority_levels": ["low"]},
        {"id": "spec-mixed", "name": "2卡40核", "allowed_priority_levels": ["LOW", " high "]},
    ]
)


def _stub_schedule_config(monkeypatch, payload: dict, sent: list | None = None) -> None:
    def _request(_session, method, path, **kwargs):  # noqa: ANN001
        if sent is not None:
            sent.append({"method": method, "path": path, **kwargs})
        return {"Result": payload}

    monkeypatch.setattr(api, "_request_json", _request)
    monkeypatch.setattr(api, "_get_base_url", lambda: "https://platform.invalid")


def test_quota_priority_levels_decodes_the_json_encoded_menu(monkeypatch) -> None:
    sent: list = []
    _stub_schedule_config(monkeypatch, {"predef_train_spec": _TRAIN_SPEC_MENU}, sent)

    levels = api.get_quota_priority_levels(
        "ws-1", spec_field="predef_train_spec", session=object()  # type: ignore[arg-type]
    )

    assert levels == {
        # `null`, `[]` and an absent key are all "no restriction declared".
        "spec-any-null": (),
        "spec-any-empty": (),
        "spec-any-missing": (),
        "spec-low": ("low",),
        "spec-mixed": ("high", "low"),
    }
    # One request for the whole workspace, PascalCase key, notebook Action --
    # `workspace.GetScheduleConfig` is admin-only and answers AccessForbidden.
    assert len(sent) == 1
    assert sent[0]["path"] == "/api/v2/notebook?Action=GetScheduleConfig"
    assert sent[0]["body"] == {"WorkspaceId": "ws-1"}


def test_quota_priority_levels_reads_an_empty_menu_as_no_rows(monkeypatch) -> None:
    # A workload the workspace publishes nothing for is an empty menu, and
    # every spec then falls through to the caller's "no statement" branch.
    _stub_schedule_config(monkeypatch, {"rayjob_quota": "", "predef_train_spec": None})

    for field in ("rayjob_quota", "predef_train_spec", "serving_quota"):
        assert (
            api.get_quota_priority_levels(
                "ws-1", spec_field=field, session=object()  # type: ignore[arg-type]
            )
            == {}
        )


def test_quota_priority_levels_survives_an_undecodable_menu(monkeypatch) -> None:
    _stub_schedule_config(monkeypatch, {"quota": "not json at all"})

    assert (
        api.get_quota_priority_levels(
            "ws-1", spec_field="quota", session=object()  # type: ignore[arg-type]
        )
        == {}
    )


def test_quota_priority_levels_propagates_a_platform_refusal(monkeypatch) -> None:
    """A read that failed must reach the caller, not look like an empty menu."""
    monkeypatch.setattr(
        api,
        "_request_json",
        lambda *_args, **_kwargs: {"code": 403, "message": "permission denied", "data": {}},
    )
    monkeypatch.setattr(api, "_get_base_url", lambda: "https://platform.invalid")

    with pytest.raises(ValueError, match="permission denied"):
        api.get_quota_priority_levels(
            "ws-1", spec_field="quota", session=object()  # type: ignore[arg-type]
        )


def test_quota_priority_spec_fields_cover_the_workloads_that_have_a_menu() -> None:
    assert api.QUOTA_PRIORITY_SPEC_FIELDS == {
        "notebook": "quota",
        "job": "predef_train_spec",
        "ray": "rayjob_quota",
        "serving": "serving_quota",
    }
    # HPC's spec list lives in a different Action entirely, so it has no menu
    # here and must never be given one by accident.
    assert "hpc" not in api.QUOTA_PRIORITY_SPEC_FIELDS


def test_quota_priority_levels_requires_a_workspace_and_a_field() -> None:
    with pytest.raises(ValueError, match="Workspace selection is required"):
        api.get_quota_priority_levels(
            "", spec_field="quota", session=object()  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="spec field is required"):
        api.get_quota_priority_levels(
            "ws-1", spec_field="", session=object()  # type: ignore[arg-type]
        )


def test_list_compute_groups_rejects_nonzero_api_code(monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "_request_json",
        lambda *_args, **_kwargs: {
            "code": 403,
            "message": "permission denied",
            "data": {},
        },
    )

    with pytest.raises(ValueError, match="permission denied"):
        api.list_compute_groups(
            workspace_id="workspace-one",
            session=object(),  # type: ignore[arg-type]
        )


def _node(name: str, **overrides: object) -> dict:
    row = {
        "node_name": name,
        "logic_compute_group_name": "H200-1号机房",
        "status": "Ready",
        "gpu": {"total": 8},
    }
    row.update(overrides)
    return row


# One idle-looking node per reason the scheduler will still refuse it.
_UNSCHEDULABLE_NODES = [
    _node("cordoned", cordon_type="Manual"),
    _node("maintenance", is_maint=True),
    _node("faulted", resource_pool="fault"),
]


def test_free_node_counts_skip_nodes_the_scheduler_cannot_place_on(monkeypatch) -> None:
    nodes = [_node("free")] + _UNSCHEDULABLE_NODES
    monkeypatch.setattr(api, "list_node_dimension", lambda *_a, **_k: nodes)

    summary = api._compute_node_summary(nodes)
    assert summary["ready_nodes"] == 4
    assert summary["free_nodes"] == 1

    counts = api.get_full_free_node_counts(
        ["lcg-1"],
        workspace_id_by_group={"lcg-1": "ws-1"},
        session=object(),  # type: ignore[arg-type]
    )
    assert [(row.ready_nodes, row.full_free_nodes) for row in counts] == [(4, 1)]


def test_free_node_counts_reuse_prefetched_dimensions(monkeypatch) -> None:
    nodes = [_node("free"), _node("busy", task_list=[{"name": "task"}])]
    monkeypatch.setattr(
        api,
        "list_node_dimension",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("prefetched node dimensions must avoid a second live read")
        ),
    )

    counts = api.get_full_free_node_counts(
        ["lcg-1"],
        workspace_id_by_group={"lcg-1": "ws-1"},
        node_dimensions_by_group={"lcg-1": nodes},
        session=object(),  # type: ignore[arg-type]
    )

    assert [(row.total_nodes, row.full_free_nodes) for row in counts] == [(2, 1)]


def test_availability_loads_compute_groups_with_bounded_concurrency(monkeypatch) -> None:
    groups = [
        {"logic_compute_group_id": f"lcg-{index}", "name": f"Group {index}"}
        for index in range(4)
    ]
    barrier = threading.Barrier(4)
    lock = threading.Lock()
    active = 0
    max_active = 0

    monkeypatch.setattr(api, "list_compute_groups", lambda **_kwargs: groups)
    monkeypatch.setattr(
        api,
        "list_node_dimension",
        lambda *_args, **_kwargs: [_node("free")],
    )

    def _request(_session, _method, path, *, referer, body, timeout):
        nonlocal active, max_active
        assert path.endswith("Action=GetLogicComputeGroupResource")
        with lock:
            active += 1
            max_active = max(max_active, active)
        barrier.wait(timeout=2)
        with lock:
            active -= 1
        return {
            "Result": {
                "logic_resouces": {"gpu_total": 8, "gpu_used": 0},
                "gpu_type_stats": [
                    {"gpu_info": {"gpu_type_display": "H200"}}
                ],
            }
        }

    monkeypatch.setattr(api, "_request_json", _request)

    class _Session:
        all_workspace_names = {"ws-1": "Workspace"}

    rows = api.get_accurate_resource_availability(
        workspace_id="ws-1",
        session=_Session(),  # type: ignore[arg-type]
    )

    assert max_active == 4
    assert [row.group_name for row in rows] == [f"Group {index}" for index in range(4)]


def test_zero_guarantee_gpu_group_is_not_hidden_as_cpu(monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "list_compute_groups",
        lambda **_kwargs: [
            {"logic_compute_group_id": "lcg-gpu", "name": "Fair H200"}
        ],
    )
    monkeypatch.setattr(
        api,
        "list_node_dimension",
        lambda *_args, **_kwargs: [_node("gpu-node", gpu_total=8)],
    )
    monkeypatch.setattr(
        api,
        "_request_json",
        lambda *_args, **_kwargs: {
            "Result": {
                "logic_resouces": {
                    "gpu_total": 0,
                    "gpu_used": 7,
                    "gpu_low_priority_used": 2,
                },
                "gpu_type_stats": [
                    {
                        "gpu_info": {
                            "gpu_type": "NVIDIA_H200_SXM_141G",
                            "gpu_type_display": "NVIDIA H200 (141GB)",
                        }
                    }
                ],
            }
        },
    )

    class _Session:
        all_workspace_names = {"ws-1": "Workspace"}

    rows = api.get_accurate_resource_availability(
        workspace_id="ws-1",
        session=_Session(),  # type: ignore[arg-type]
    )

    assert len(rows) == 1
    assert rows[0].resource_kind == "gpu"
    assert rows[0].gpu_type == "NVIDIA H200 (141GB)"
    assert rows[0].available_gpus == -7
    assert rows[0].gpu_per_node == 8
