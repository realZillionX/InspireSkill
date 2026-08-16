"""Wrapper contract for the six `train.*Tensorboard*` Actions and the board app."""

from __future__ import annotations

from typing import Any

import pytest

from inspire.platform.web.browser_api import tensorboards as tb_module


def _capture(monkeypatch: pytest.MonkeyPatch, result: Any) -> dict:
    sent: dict = {}

    def _fake(session, method, path, *, referer, body=None, timeout=30):  # noqa: ANN001
        sent["path"] = path
        sent["body"] = body
        sent["referer"] = referer
        return {"Result": result}

    monkeypatch.setattr(tb_module, "_request_json", _fake)
    return sent


def test_list_tensorboards_uses_the_pascal_case_page_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ListTensorboards` reads `PageNumber`; `page` and `page_num` are ignored."""
    sent = _capture(monkeypatch, {"items": [], "total": "0"})
    tb_module.list_tensorboards(
        workspace_id="ws-1", created_by="user-1", page_num=2, page_size=7, session=object()
    )

    assert "Action=ListTensorboards" in sent["path"]
    assert sent["body"]["PageNumber"] == 2
    assert sent["body"]["page_size"] == 7
    # Without it the Action reports a workspace-wide total that the returned
    # rows cannot add up to.
    assert sent["body"]["created_by"] == "user-1"
    assert "page" not in sent["body"] and "page_num" not in sent["body"]


def test_list_tensorboards_sends_status_as_one_platform_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A list is rejected by the unmarshaller; the field is a bare string."""
    sent = _capture(monkeypatch, {"items": [], "total": 0})
    tb_module.list_tensorboards(
        workspace_id="ws-1",
        created_by="user-1",
        status="running",
        keyword="glm",
        session=object(),
    )

    assert sent["body"]["status"] == "tb_status_running"
    assert sent["body"]["keyword"] == "glm"


def test_list_tensorboards_omits_absent_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _capture(monkeypatch, {"items": [], "total": 0})
    tb_module.list_tensorboards(workspace_id="ws-1", created_by="user-1", session=object())

    assert "status" not in sent["body"] and "keyword" not in sent["body"]


def test_list_tensorboards_projects_rows_without_the_status_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(
        monkeypatch,
        {
            "items": [
                {
                    "tb_id": "tb-a",
                    "name": "board-a",
                    "status": "tb_status_running",
                    "job_name": "train-a",
                    "tb_summary_path": "/inspire/hdd/project/p/u/logs",
                    "logic_compute_group_name": "H200",
                    "project_name": "研究项目",
                    "auto_stop_time_ms": "86400000",
                    "url": "https://notebook-inspire.example/tensorboard/tb-a/",
                    "created_at": "1769591284000",
                }
            ],
            "total": "6",
        },
    )
    boards, total = tb_module.list_tensorboards(
        workspace_id="ws-1", created_by="user-1", session=object()
    )

    assert total == 6
    assert boards[0].status == "running"
    assert boards[0].summary_path == "/inspire/hdd/project/p/u/logs"
    assert boards[0].job_name == "train-a"
    assert boards[0].tb_id == "tb-a"


def test_list_tensorboards_requires_a_workspace() -> None:
    with pytest.raises(ValueError, match="Workspace"):
        tb_module.list_tensorboards(workspace_id=None, created_by="u", session=object())


def test_create_sends_the_auto_stop_field_as_a_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auto_stop_time_ms` is a proto string field; an int is rejected outright."""
    sent = _capture(monkeypatch, {})
    tb_module.create_tensorboard(
        name="board-a",
        workspace_id="ws-1",
        project_id="project-1",
        logic_compute_group_id="lcg-1",
        summary_path="/inspire/hdd/project/p/u/logs",
        auto_stop_ms=3_600_000,
        session=object(),
    )

    assert "Action=CreateTensorboard" in sent["path"]
    assert sent["body"]["auto_stop_time_ms"] == "3600000"
    assert isinstance(sent["body"]["auto_stop_time_ms"], str)
    # A standalone board must not carry an empty job handle.
    assert "job_id" not in sent["body"]


def test_create_carries_a_job_handle_when_one_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _capture(monkeypatch, {})
    tb_module.create_tensorboard(
        name="board-a",
        workspace_id="ws-1",
        project_id="project-1",
        logic_compute_group_id="lcg-1",
        summary_path="/logs",
        auto_stop_ms=3_600_000,
        job_id="job-1",
        session=object(),
    )

    assert sent["body"]["job_id"] == "job-1"


@pytest.mark.parametrize(
    "field,value",
    [("name", ""), ("summary_path", ""), ("project_id", ""), ("logic_compute_group_id", "")],
)
def test_create_rejects_the_fields_the_platform_would_silently_accept(
    field: str, value: str
) -> None:
    """The gateway takes a nameless or path-less board and makes it unusable."""
    kwargs = {
        "name": "board-a",
        "workspace_id": "ws-1",
        "project_id": "project-1",
        "logic_compute_group_id": "lcg-1",
        "summary_path": "/logs",
        "auto_stop_ms": 3_600_000,
        "session": object(),
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        tb_module.create_tensorboard(**kwargs)  # type: ignore[arg-type]


def test_create_rejects_an_auto_stop_over_the_platform_ceiling() -> None:
    """Above 72h the platform answers `must less than 72h0m0s`."""
    with pytest.raises(ValueError, match="72h"):
        tb_module.create_tensorboard(
            name="board-a",
            workspace_id="ws-1",
            project_id="project-1",
            logic_compute_group_id="lcg-1",
            summary_path="/logs",
            auto_stop_ms=tb_module.MAX_AUTO_STOP_MS + 1,
            session=object(),
        )


@pytest.mark.parametrize(
    "call,action",
    [
        (tb_module.get_tensorboard, "GetTensorboard"),
        (tb_module.start_tensorboard, "StartTensorboard"),
        (tb_module.stop_tensorboard, "StopTensorboard"),
        (tb_module.delete_tensorboard, "DeleteTensorboard"),
    ],
)
def test_handle_actions_send_tb_id(
    monkeypatch: pytest.MonkeyPatch, call, action: str  # noqa: ANN001
) -> None:
    sent = _capture(monkeypatch, {"tb_id": "tb-a", "name": "board-a"})
    call("tb-a", session=object())

    assert f"Action={action}" in sent["path"]
    assert sent["body"] == {"tb_id": "tb-a"}


@pytest.mark.parametrize(
    "call",
    [
        tb_module.get_tensorboard,
        tb_module.start_tensorboard,
        tb_module.stop_tensorboard,
        tb_module.delete_tensorboard,
    ],
)
def test_handle_actions_reject_an_empty_handle(call) -> None:  # noqa: ANN001
    with pytest.raises(ValueError, match="TensorBoard id"):
        call("", session=object())


def test_app_url_absolutizes_the_site_relative_form() -> None:
    """Older rows carry a path, newer ones an absolute address on another host."""
    monkey = tb_module._get_base_url
    try:
        tb_module._get_base_url = lambda: "https://qz.example"  # type: ignore[assignment]
        assert (
            tb_module.tensorboard_app_url("/api/v1/train_job/tensorboard/tb-a/")
            == "https://qz.example/api/v1/train_job/tensorboard/tb-a/"
        )
    finally:
        tb_module._get_base_url = monkey  # type: ignore[assignment]

    assert (
        tb_module.tensorboard_app_url("https://notebook.example/tensorboard/tb-a")
        == "https://notebook.example/tensorboard/tb-a/"
    )


def test_app_url_rejects_a_board_with_no_address() -> None:
    with pytest.raises(ValueError, match="no address"):
        tb_module.tensorboard_app_url("")


def test_scalar_series_is_projected_into_wall_step_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tb_module,
        "_tensorboard_get",
        lambda *a, **k: [[1775040043.25, 0, 0.83], [1775042968.5, 1, 0.84], ["bad"]],
    )
    points = tb_module.read_tensorboard_scalar_series(
        "https://x/tb-a/", run=".", tag="eval/x", session=object()
    )

    assert points == [(1775040043.25, 0, 0.83), (1775042968.5, 1, 0.84)]


def test_scalar_tags_are_sorted_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tb_module,
        "_tensorboard_get",
        lambda *a, **k: {".": {"loss": {}, "acc": {}}, "run-2": "not-a-mapping"},
    )
    tags = tb_module.read_tensorboard_scalar_tags("https://x/tb-a/", session=object())

    assert tags == {".": ["acc", "loss"]}
