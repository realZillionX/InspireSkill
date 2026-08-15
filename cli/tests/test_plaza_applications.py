"""数据广场 access applications: request shapes, state words, and resolution."""

from __future__ import annotations

from typing import Any

import pytest

from inspire.platform.web.plaza import applications as plaza_applications


def _row(dataset: str = "pixabay-81k", **overrides: Any) -> dict[str, Any]:
    row = {
        "id": 42,
        "datasetName": dataset,
        "authorityName": "只读",
        "applyTime": "2026-08-01 09:12:00",
        "approveTime": "",
        "approveUser": "",
        "applyDescr": "训练视频生成模型需要这份数据。",
        "state": 0,
    }
    row.update(overrides)
    return row


def _install(monkeypatch: pytest.MonkeyPatch, *payloads: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    queue = list(payloads)

    def fake_plaza_request(method, path, *, params=None, body=None, session=None, timeout=30):  # noqa: ANN001
        calls.append({"method": method, "path": path, "params": params, "body": body})
        return queue.pop(0) if queue else {}

    monkeypatch.setattr(plaza_applications, "plaza_request", fake_plaza_request)
    return calls


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_dataset_applications_sends_paging_and_search(monkeypatch) -> None:  # noqa: ANN001
    calls = _install(monkeypatch, {"list": [_row()], "total": 1, "page": 1, "pageSize": 5})

    items, total = plaza_applications.list_dataset_applications(
        keyword="  pixabay  ", page=2, page_size=5
    )

    assert calls[0]["method"] == "GET"
    assert calls[0]["path"] == "/api/datasetApplyApprove/getDatasetApplyList"
    assert calls[0]["params"] == {"page": 2, "pageSize": 5, "keyword": "pixabay"}
    assert total == 1
    assert [item.dataset for item in items] == ["pixabay-81k"]


def test_list_dataset_applications_omits_an_empty_keyword(monkeypatch) -> None:  # noqa: ANN001
    calls = _install(monkeypatch, {"list": [], "total": 0})

    plaza_applications.list_dataset_applications(keyword="   ")

    assert "keyword" not in calls[0]["params"]


def test_an_empty_page_is_an_answer_not_a_failure(monkeypatch) -> None:  # noqa: ANN001
    # The live envelope for an account that has never applied for anything.
    _install(monkeypatch, {"list": [], "total": 0, "page": 1, "pageSize": 5})

    items, total = plaza_applications.list_dataset_applications()

    assert items == []
    assert total == 0


def test_list_dataset_approvals_uses_the_approver_endpoint(monkeypatch) -> None:  # noqa: ANN001
    calls = _install(
        monkeypatch,
        {
            "list": [
                _row(applyUser="张三", projectName="多模态基础架构", state=0)
            ],
            "total": 1,
        },
    )

    items, _total = plaza_applications.list_dataset_approvals()

    assert calls[0]["path"] == "/api/datasetApplyApprove/getDatasetApproveList"
    # The plaza's own front end also sends a `role` narrowing; its semantics
    # are not established, so it is neither sent nor exposed.
    assert set(calls[0]["params"]) == {"page", "pageSize"}
    assert items[0].applicant == "张三"
    assert items[0].project == "多模态基础架构"


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "word"),
    ((0, "pending"), (1, "approved"), (2, "rejected"), (-1, "withdrawn")),
)
def test_every_state_is_reported_as_a_word(monkeypatch, code, word) -> None:  # noqa: ANN001
    _install(monkeypatch, {"list": [_row(state=code)], "total": 1})

    items, _total = plaza_applications.list_dataset_applications()

    assert items[0].state == word


def test_a_string_state_reads_the_same_as_an_int(monkeypatch) -> None:  # noqa: ANN001
    _install(monkeypatch, {"list": [_row(state="1")], "total": 1})

    items, _total = plaza_applications.list_dataset_applications()

    assert items[0].state == "approved"


def test_a_missing_state_does_not_become_pending(monkeypatch) -> None:  # noqa: ANN001
    # `0` is a real state, so an absent value must not fall through onto it.
    _install(monkeypatch, {"list": [_row(state=None)], "total": 1})

    items, _total = plaza_applications.list_dataset_applications()

    assert items[0].state == ""


def test_an_unmapped_state_is_reported_as_sent(monkeypatch) -> None:  # noqa: ANN001
    _install(monkeypatch, {"list": [_row(state=9)], "total": 1})

    items, _total = plaza_applications.list_dataset_applications()

    assert items[0].state == "9"


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


def test_get_dataset_application_reads_the_mountable_code(monkeypatch) -> None:  # noqa: ANN001
    calls = _install(
        monkeypatch,
        _row(datasetCode="pixabay-81k", datasetName="Pixabay 81K", state=1),
    )

    application = plaza_applications.get_dataset_application(42)

    assert calls[0]["method"] == "GET"
    assert calls[0]["path"] == "/api/datasetApplyApprove/intoApproveById"
    assert calls[0]["params"] == {"id": 42}
    # The detail view carries `datasetCode`, which is the value a mount takes;
    # a listing row only names its dataset.
    assert application.dataset == "pixabay-81k"
    assert application.state == "approved"


def test_get_dataset_application_refuses_an_unusable_handle(monkeypatch) -> None:  # noqa: ANN001
    calls = _install(monkeypatch, {})

    with pytest.raises(plaza_applications.UnknownDatasetApplicationError):
        plaza_applications.get_dataset_application(0)

    assert calls == []


def test_get_dataset_application_refuses_a_non_record(monkeypatch) -> None:  # noqa: ANN001
    _install(monkeypatch, [])

    with pytest.raises(plaza_applications.UnknownDatasetApplicationError):
        plaza_applications.get_dataset_application(42)


# ---------------------------------------------------------------------------
# Resolution by dataset name
# ---------------------------------------------------------------------------


def test_find_dataset_applications_takes_the_exact_match_not_the_first_hit(
    monkeypatch,  # noqa: ANN001
) -> None:
    calls = _install(
        monkeypatch,
        {
            "list": [
                _row("pixabay-81k-mini", id=7),
                _row("pixabay-81k", id=8),
            ],
            "total": 2,
        },
        _row("pixabay-81k", id=8, datasetCode="pixabay-81k", state=1),
    )

    found = plaza_applications.find_dataset_applications("pixabay-81k")

    # The plaza's keyword search matches descriptions too, so the answer is the
    # row whose dataset is exactly the one asked for.
    assert calls[0]["params"]["keyword"] == "pixabay-81k"
    assert calls[1]["params"] == {"id": 8}
    assert [item.dataset for item in found] == ["pixabay-81k"]
    assert found[0].state == "approved"


def test_find_dataset_applications_reads_every_record_for_one_dataset(
    monkeypatch,  # noqa: ANN001
) -> None:
    calls = _install(
        monkeypatch,
        {"list": [_row(id=1, state=2), _row(id=2, state=0)], "total": 2},
        _row(id=1, state=2),
        _row(id=2, state=0),
    )

    found = plaza_applications.find_dataset_applications("pixabay-81k")

    assert [call["params"].get("id") for call in calls[1:]] == [1, 2]
    assert [item.state for item in found] == ["rejected", "pending"]


def test_find_dataset_applications_bounds_the_detail_requests(monkeypatch) -> None:  # noqa: ANN001
    rows = [_row(id=index + 1) for index in range(25)]
    calls = _install(monkeypatch, {"list": rows, "total": 25}, *rows)

    plaza_applications.find_dataset_applications("pixabay-81k", limit=5)

    assert len([call for call in calls if "id" in (call["params"] or {})]) == 5


def test_find_dataset_applications_can_search_the_approver_side(monkeypatch) -> None:  # noqa: ANN001
    calls = _install(monkeypatch, {"list": [_row(id=3)], "total": 1}, _row(id=3))

    plaza_applications.find_dataset_applications("pixabay-81k", incoming=True)

    assert calls[0]["path"] == "/api/datasetApplyApprove/getDatasetApproveList"


def test_find_dataset_applications_raises_when_nothing_matches(monkeypatch) -> None:  # noqa: ANN001
    _install(monkeypatch, {"list": [_row("other-dataset")], "total": 1})

    with pytest.raises(
        plaza_applications.UnknownDatasetApplicationError,
        match="pixabay-81k",
    ):
        plaza_applications.find_dataset_applications("pixabay-81k")


def test_find_dataset_applications_requires_a_name(monkeypatch) -> None:  # noqa: ANN001
    calls = _install(monkeypatch)

    with pytest.raises(plaza_applications.UnknownDatasetApplicationError):
        plaza_applications.find_dataset_applications("   ")

    assert calls == []


# ---------------------------------------------------------------------------
# Write endpoints stay unwired
# ---------------------------------------------------------------------------


_READ_ONLY_PATHS = {
    "/api/datasetApplyApprove/getDatasetApplyList",
    "/api/datasetApplyApprove/getDatasetApproveList",
    "/api/datasetApplyApprove/intoApproveById",
}


def test_every_call_this_module_makes_is_one_of_three_reads(monkeypatch) -> None:  # noqa: ANN001
    # Applying and approving reach a human reviewer under the caller's name,
    # and granting a role is an administrative act; all three are left to the
    # web UI. Nothing here may issue anything but these three GETs.
    calls = _install(
        monkeypatch,
        {"list": [_row(id=1)], "total": 1},
        {"list": [_row(id=1)], "total": 1},
        _row(id=1),
        {"list": [_row(id=1)], "total": 1},
        _row(id=1),
    )

    plaza_applications.list_dataset_applications()
    plaza_applications.list_dataset_approvals()
    plaza_applications.get_dataset_application(1)
    plaza_applications.find_dataset_applications("pixabay-81k")

    assert calls
    for call in calls:
        assert call["method"] == "GET"
        assert call["path"] in _READ_ONLY_PATHS
        assert call["body"] is None


def test_the_module_exports_no_write_helper() -> None:
    assert not [
        name
        for name in plaza_applications.__all__
        if name.startswith(("apply", "approve", "reject", "withdraw", "grant", "create"))
    ]
