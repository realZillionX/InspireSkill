"""Unit tests for `inspire.platform.web.browser_api.batch_query`.

These pin the parts of the batch contract that were measured against the live
platform on 2026-08-18 and that a plausible-looking refactor would silently
break:

- ids are deduplicated *before* chunking, because the cap counts the list and
  not the set: twenty unique ids plus one repeat is rejected as twenty-one
- an empty id list makes no request at all, because a `job_ids: []` body falls
  through to the paging path and fails there about page size
- `ListJobs` drops ids it cannot find, so they are absent from the mapping
- `ListJobEvents` fails the *whole* request for one unknown id, and the
  recovery reports that id rather than returning the empty list that would
  read as "these jobs have no events"
"""

from __future__ import annotations

import pytest

from inspire.platform.web.browser_api import batch_query
from inspire.platform.web.browser_api.batch_query import (
    BATCH_ID_LIMIT,
    dedupe_ids,
    fetch_events_by_ids,
    fetch_jobs_by_ids,
)


class _FakeSession:
    def __init__(self) -> None:
        self.workspace_id = "ws-fake"


def _install(monkeypatch: pytest.MonkeyPatch, handler, calls: list[dict]) -> None:
    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):
        calls.append({"url": url, "body": body, "referer": referer})
        return handler(body)

    monkeypatch.setattr(batch_query, "_request_json", _fake)


def _ok(result: dict) -> dict:
    return {"ResponseMetadata": {}, "Result": result}


def _error(code: str, message: str) -> dict:
    return {"ResponseMetadata": {"Error": {"Code": code, "Message": message}}}


def test_dedupe_keeps_first_occurrence_order() -> None:
    assert dedupe_ids([" a ", "b", "a", "", None, "c"]) == ["a", "b", "c"]


def test_jobs_chunk_at_the_platform_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = [f"job-{index}" for index in range(45)]
    calls: list[dict] = []
    _install(
        monkeypatch,
        lambda body: _ok(
            {"jobs": [{"job_id": value} for value in body["job_ids"]]}
        ),
        calls,
    )

    found = fetch_jobs_by_ids(
        _FakeSession(),
        route="train",
        workspace_id="ws-1",
        job_ids=ids,
        referer="https://example/ref",
    )

    assert len(calls) == 3
    assert [len(call["body"]["job_ids"]) for call in calls] == [20, 20, 5]
    assert all(len(call["body"]["job_ids"]) <= BATCH_ID_LIMIT for call in calls)
    assert set(found) == set(ids)


def test_repeats_do_not_consume_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """20 unique ids plus repeats is one request, not a rejected 21-item one."""
    ids = [f"job-{index}" for index in range(20)] + ["job-0", "job-5"]
    calls: list[dict] = []
    _install(
        monkeypatch,
        lambda body: _ok({"jobs": [{"job_id": v} for v in body["job_ids"]]}),
        calls,
    )

    fetch_jobs_by_ids(
        _FakeSession(),
        route="train",
        workspace_id="ws-1",
        job_ids=ids,
        referer="https://example/ref",
    )

    assert len(calls) == 1
    assert len(calls[0]["body"]["job_ids"]) == 20


def test_empty_ids_make_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    _install(monkeypatch, lambda body: _ok({"jobs": []}), calls)

    assert (
        fetch_jobs_by_ids(
            _FakeSession(),
            route="train",
            workspace_id="ws-1",
            job_ids=[],
            referer="https://example/ref",
        )
        == {}
    )
    assert calls == []


def test_jobs_require_a_workspace() -> None:
    with pytest.raises(ValueError, match="workspace_id is required"):
        fetch_jobs_by_ids(
            _FakeSession(),
            route="train",
            workspace_id="",
            job_ids=["job-1"],
            referer="https://example/ref",
        )


def test_unknown_job_ids_are_absent_rather_than_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The platform drops them silently; the caller has to be able to tell."""
    calls: list[dict] = []
    _install(
        monkeypatch,
        lambda body: _ok({"jobs": [{"job_id": "job-live"}]}),
        calls,
    )

    found = fetch_jobs_by_ids(
        _FakeSession(),
        route="train",
        workspace_id="ws-1",
        job_ids=["job-live", "job-gone"],
        referer="https://example/ref",
    )

    assert set(found) == {"job-live"}
    assert "job-gone" not in found


def test_events_are_split_back_out_by_object_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    _install(
        monkeypatch,
        lambda body: _ok(
            {
                "total": 3,
                "events": [
                    {"object_id": "job-a", "reason": "One"},
                    {"object_id": "job-b", "reason": "Two"},
                    {"object_id": "job-a", "reason": "Three"},
                ],
            }
        ),
        calls,
    )

    events, missing = fetch_events_by_ids(
        _FakeSession(),
        route="train",
        object_type="job",
        object_ids=["job-a", "job-b"],
        referer="https://example/ref",
    )

    assert missing == []
    assert [row["reason"] for row in events["job-a"]] == ["One", "Three"]
    assert [row["reason"] for row in events["job-b"]] == ["Two"]


def test_job_with_no_events_keeps_an_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quiet job and an unknown job must not look the same."""
    calls: list[dict] = []
    _install(
        monkeypatch,
        lambda body: _ok(
            {"total": 1, "events": [{"object_id": "job-a", "reason": "One"}]}
        ),
        calls,
    )

    events, missing = fetch_events_by_ids(
        _FakeSession(),
        route="train",
        object_type="job",
        object_ids=["job-a", "job-quiet"],
        referer="https://example/ref",
    )

    assert missing == []
    assert events["job-quiet"] == []


def test_one_unknown_id_does_not_lose_the_other_nineteen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ListJobEvents` fails the whole batch; the retry must rescue the rest."""
    calls: list[dict] = []

    def _handler(body: dict) -> dict:
        sent = body["filter"]["object_ids"]
        if "job-gone" in sent:
            return _error("InvalidParameter", "job job-gone not found")
        return _ok(
            {
                "total": len(sent),
                "events": [{"object_id": value, "reason": "Ok"} for value in sent],
            }
        )

    _install(monkeypatch, _handler, calls)

    events, missing = fetch_events_by_ids(
        _FakeSession(),
        route="train",
        object_type="job",
        object_ids=["job-a", "job-gone", "job-b"],
        referer="https://example/ref",
    )

    assert missing == ["job-gone"]
    assert set(events) == {"job-a", "job-b"}
    assert [row["reason"] for row in events["job-a"]] == ["Ok"]
    # First attempt carried all three, the retry dropped only the named id.
    assert calls[0]["body"]["filter"]["object_ids"] == ["job-a", "job-gone", "job-b"]
    assert calls[1]["body"]["filter"]["object_ids"] == ["job-a", "job-b"]


def test_unnamed_not_found_falls_back_to_one_request_per_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message that names no id still has to identify the dead one."""
    calls: list[dict] = []

    def _handler(body: dict) -> dict:
        sent = body["filter"]["object_ids"]
        if len(sent) > 1:
            return _error("InvalidParameter", "object not found")
        if sent == ["job-gone"]:
            return _error("InvalidParameter", "object not found")
        return _ok(
            {"total": 1, "events": [{"object_id": sent[0], "reason": "Ok"}]}
        )

    _install(monkeypatch, _handler, calls)

    events, missing = fetch_events_by_ids(
        _FakeSession(),
        route="train",
        object_type="job",
        object_ids=["job-a", "job-gone"],
        referer="https://example/ref",
    )

    assert missing == ["job-gone"]
    assert [row["reason"] for row in events["job-a"]] == ["Ok"]


def test_non_missing_failures_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A throttled platform must not read as a batch of jobs with no events."""
    calls: list[dict] = []
    _install(
        monkeypatch,
        lambda body: _error("InvalidParameter", "object_ids count exceeds limit 20"),
        calls,
    )

    with pytest.raises(ValueError, match="exceeds limit"):
        fetch_events_by_ids(
            _FakeSession(),
            route="train",
            object_type="job",
            object_ids=["job-a"],
            referer="https://example/ref",
        )


def test_a_transient_failure_whose_text_says_not_found_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TransientAPIError` subclasses `ValueError`, and a 5xx body can carry
    any words -- "not found" included. A platform that did not answer has not
    said any job is missing, so the not-found recovery must not eat it and
    report the chunk as dead jobs."""
    from inspire.platform.web.session import TransientAPIError

    calls: list[dict] = []
    _install(
        monkeypatch,
        lambda body: _error("InternalError", "backend shard not found"),
        calls,
    )

    with pytest.raises(TransientAPIError):
        fetch_events_by_ids(
            _FakeSession(),
            route="train",
            object_type="job",
            object_ids=["job-a", "job-b"],
            referer="https://example/ref",
        )

    # And no per-id fallback fan-out either: the answer was "ask again later",
    # not "one of these is dead".
    assert len(calls) == 1


def test_events_page_until_total_is_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    pages = {
        1: [{"object_id": "job-a", "reason": f"r{index}"} for index in range(200)],
        2: [{"object_id": "job-a", "reason": "tail"}],
    }

    def _handler(body: dict) -> dict:
        return _ok({"total": 201, "events": pages[body["page_num"]]})

    _install(monkeypatch, _handler, calls)

    events, missing = fetch_events_by_ids(
        _FakeSession(),
        route="train",
        object_type="job",
        object_ids=["job-a"],
        referer="https://example/ref",
    )

    assert missing == []
    assert len(events["job-a"]) == 201
    assert [call["body"]["page_num"] for call in calls] == [1, 2]


def test_hpc_paging_uses_camel_case_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """`hpc.ListJobEvents` wants `pageNum`; `page_num` silently re-reads page 1."""
    calls: list[dict] = []
    _install(monkeypatch, lambda body: _ok({"total": 0, "events": []}), calls)

    fetch_events_by_ids(
        _FakeSession(),
        route="hpc",
        object_type="HPC_JOB",
        object_ids=["hpc-a"],
        referer="https://example/ref",
        page_key="pageNum",
        page_size_key="pageSize",
        sorter=[{"field": "last_timestamp", "sort": "ascend"}],
    )

    body = calls[0]["body"]
    assert "pageNum" in body and "page_num" not in body
    assert "pageSize" in body and "page_size" not in body
    assert body["filter"]["object_type"] == "HPC_JOB"
    assert calls[0]["url"] == "/api/v2/hpc?Action=ListJobEvents"
