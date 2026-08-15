"""Wrapper contract for ``hpc.ListSlurmdPodEvent`` (per-pod HPC events)."""

from __future__ import annotations

from typing import Any

import pytest

from inspire.platform.web.browser_api import hpc_jobs as hpc_jobs_module
from inspire.platform.web.browser_api.hpc_jobs import list_hpc_instance_events
from inspire.platform.web.session import SessionExpiredError, TransientAPIError


class _FakeSession:
    workspace_id = "ws-default"


def _install_responses(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[dict],
    calls: list[dict[str, Any]],
) -> None:
    queue = list(responses)

    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):
        calls.append({"url": url, "referer": referer, "body": body})
        return queue.pop(0) if queue else {"Result": {"events": [], "total": "0"}}

    monkeypatch.setattr(hpc_jobs_module, "_request_json", _fake)


def _event(reason: str, last: str = "1773389457000") -> dict[str, Any]:
    return {
        "reason": reason,
        "message": f"{reason} happened",
        "from": "kubelet",
        "first_timestamp": "1773388870000",
        "last_timestamp": last,
        "object_id": "ns/pod-0",
        "object_type": "HPC_JOB_INSTANCE",
    }


def test_posts_instance_id_with_a_mandatory_page_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting `page_size` answers `events: []` next to a non-zero `total`."""
    calls: list[dict[str, Any]] = []
    _install_responses(
        monkeypatch,
        [{"Result": {"events": [_event("BackOff")], "total": "1"}}],
        calls,
    )

    events = list_hpc_instance_events(
        ["exploration-topic/hpc-job-1-cluster-slurmd-0"],
        _FakeSession(),
        job_id="hpc-job-1",
        page_size=20,
    )

    assert [event["reason"] for event in events] == ["BackOff"]
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/api/v2/hpc?Action=ListSlurmdPodEvent")
    assert calls[0]["referer"].endswith("/jobs/hpcDetail/hpc-job-1")
    assert calls[0]["body"] == {
        "instance_id": "exploration-topic/hpc-job-1-cluster-slurmd-0",
        "page_size": 20,
        "PageNumber": 1,
    }


def test_pages_until_the_string_total_is_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`total` arrives as a string; reading it as an int stops after page one."""
    calls: list[dict[str, Any]] = []
    _install_responses(
        monkeypatch,
        [
            {"Result": {"events": [_event("A"), _event("B")], "total": "3"}},
            {"Result": {"events": [_event("C")], "total": "3"}},
        ],
        calls,
    )

    events = list_hpc_instance_events(["ns/pod-0"], _FakeSession(), page_size=2)

    assert [event["reason"] for event in events] == ["A", "B", "C"]
    assert [call["body"]["PageNumber"] for call in calls] == [1, 2]


def test_queries_every_instance_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Instances are queried concurrently, so answers key off the id, not order."""
    calls: list[dict[str, Any]] = []

    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):  # noqa: ANN001
        calls.append({"url": url, "referer": referer, "body": body})
        reason = {"ns/pod-0": "A", "ns/pod-1": "B"}[body["instance_id"]]
        return {"Result": {"events": [_event(reason)], "total": "1"}}

    monkeypatch.setattr(hpc_jobs_module, "_request_json", _fake)

    events = list_hpc_instance_events(
        ["ns/pod-0", " ns/pod-1 ", "ns/pod-0", ""],
        _FakeSession(),
        page_size=50,
    )

    # Results are reassembled in input order even though the calls race.
    assert [event["reason"] for event in events] == ["A", "B"]
    assert sorted(call["body"]["instance_id"] for call in calls) == ["ns/pod-0", "ns/pod-1"]


def test_reads_the_list_under_alternate_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _install_responses(
        monkeypatch,
        [{"Result": {"list": [_event("Scheduled")], "total": "1"}}],
        calls,
    )

    events = list_hpc_instance_events(["ns/pod-0"], _FakeSession(), page_size=50)

    assert [event["reason"] for event in events] == ["Scheduled"]


def test_no_instances_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _install_responses(monkeypatch, [], calls)

    assert list_hpc_instance_events([" ", ""], _FakeSession()) == []
    assert calls == []


def test_rejects_a_zero_page_size() -> None:
    with pytest.raises(ValueError, match="page_size must be positive"):
        list_hpc_instance_events(["ns/pod-0"], _FakeSession(), page_size=0)


def test_rejects_a_non_positive_max_pages() -> None:
    with pytest.raises(ValueError, match="max_pages must be positive"):
        list_hpc_instance_events(["ns/pod-0"], _FakeSession(), max_pages=0)


@pytest.mark.parametrize("error", [SessionExpiredError("expired"), TransientAPIError("429")])
def test_a_platform_that_did_not_answer_is_not_an_empty_timeline(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """An unanswered request must never read as "this pod has no events"."""

    def _fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(hpc_jobs_module, "_request_json", _fail)

    with pytest.raises(type(error)):
        list_hpc_instance_events(["ns/pod-0"], _FakeSession(), page_size=50)


def test_other_failures_stay_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*_args, **_kwargs):
        raise ValueError("API error: ResourceNotFound")

    monkeypatch.setattr(hpc_jobs_module, "_request_json", _fail)

    assert list_hpc_instance_events(["ns/pod-0"], _FakeSession(), page_size=50) == []
