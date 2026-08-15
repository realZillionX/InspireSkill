"""`inspire resources node-events` — the one event source keyed by node.

Its rows are spelled differently from every workload event Action (`event_type`
instead of `type`, `node_name` instead of an object handle), so these tests pin
the translation as much as the command.
"""

from __future__ import annotations

import importlib
import json

import pytest
from click.testing import CliRunner

from inspire.cli.main import main as cli_main
from inspire.cli.utils.events import public_event
from inspire.platform.web.browser_api.availability import api as availability_api

node_events_mod = importlib.import_module(
    "inspire.cli.commands.resources.resources_node_events"
)


def _node_event(
    node: str,
    reason: str,
    last: str,
    *,
    event_type: str = "Normal",
    source: str = "kubelet",
) -> dict:
    return {
        "event_type": event_type,
        "first_timestamp": last,
        "from": source,
        "last_timestamp": last,
        "message": f"{reason} on {node}",
        "node_id": f"cluster-qb-4_{node}",
        "node_name": node,
        "reason": reason,
    }


def _patch_command(monkeypatch: pytest.MonkeyPatch, events: list[dict]) -> list[list[str]]:
    asked: list[list[str]] = []
    monkeypatch.setattr(
        node_events_mod.Config,
        "from_files_and_env",
        lambda **_kwargs: (object(), {}),
    )
    monkeypatch.setattr(node_events_mod, "get_web_session", lambda: object())
    monkeypatch.setattr(
        node_events_mod.browser_api_module,
        "list_node_events",
        lambda names, **_kwargs: asked.append(list(names)) or list(events),
    )
    return asked


# --- wrapper ----------------------------------------------------------------


def test_the_node_filter_is_sent_and_paging_stops_on_a_short_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[dict] = []

    def fake_request(_session, _method, path, *, referer, body=None, timeout=30):  # noqa: ANN001
        bodies.append(body)
        assert "ListNodeEvents" in path
        assert referer.endswith("/cluster/nodeList")
        return {"code": 0, "data": {"events": [_node_event("n1", "CordonNode", "1")], "total": 1}}

    monkeypatch.setattr(availability_api, "_request_json", fake_request)

    events = availability_api.list_node_events(
        ["n1", "n2", "n1"],
        page_size=200,
        session=object(),
    )

    assert len(events) == 1
    assert len(bodies) == 1
    assert bodies[0]["filter"] == {"node_names": ["n1", "n2"]}
    assert bodies[0]["sorter"] == [{"field": "last_timestamp", "sort": "ascend"}]


def test_an_empty_node_selection_is_refused_before_the_call() -> None:
    """No filter answers `total: 0`, which reads as "the cluster is quiet"."""
    with pytest.raises(ValueError, match="Node selection is required"):
        availability_api.list_node_events([" "], session=object())


# --- projection -------------------------------------------------------------


def test_the_node_spelling_reaches_the_shared_event_schema() -> None:
    public = public_event(_node_event("gpu040", "TaskHung", "1786806809000", event_type="Warning"))

    assert public["node"] == "gpu040"
    assert public["type"] == "Warning"
    assert "node_id" not in public


# --- command ----------------------------------------------------------------


def test_several_nodes_answer_in_one_timeline_with_a_node_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_command(
        monkeypatch,
        [
            _node_event("gpu040", "UncordonNode", "3"),
            _node_event("compute531", "CordonNode", "2", event_type="Warning"),
        ],
    )

    rendered = CliRunner().invoke(
        cli_main, ["resources", "node-events", "gpu040", "compute531"]
    )
    assert rendered.exit_code == 0, rendered.output
    assert "Node" in rendered.output

    payload = CliRunner().invoke(
        cli_main, ["--json", "resources", "node-events", "gpu040", "compute531"]
    )
    assert payload.exit_code == 0, payload.output
    items = json.loads(payload.output)["data"]["items"]
    # The platform answers in filter order; the merged stream is chronological.
    assert [(item["node"], item["reason"]) for item in items] == [
        ("compute531", "CordonNode"),
        ("gpu040", "UncordonNode"),
    ]


def test_the_type_filter_reads_the_node_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`event_type` is the only place these rows say Normal or Warning."""
    _patch_command(
        monkeypatch,
        [
            _node_event("gpu040", "NodeSchedulable", "1"),
            _node_event("gpu040", "CordonNode", "2", event_type="Warning"),
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "resources", "node-events", "gpu040", "--type", "Warning"],
    )

    assert result.exit_code == 0, result.output
    items = json.loads(result.output)["data"]["items"]
    assert [item["reason"] for item in items] == ["CordonNode"]


def test_the_from_filter_narrows_to_one_reporting_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_command(
        monkeypatch,
        [
            _node_event("gpu040", "TaskHung", "1", source="kernel-monitor"),
            _node_event("gpu040", "NodeSchedulable", "2", source="kubelet"),
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "resources", "node-events", "gpu040", "--from", "kernel-monitor"],
    )

    assert result.exit_code == 0, result.output
    items = json.loads(result.output)["data"]["items"]
    assert [item["reason"] for item in items] == ["TaskHung"]


def test_a_node_name_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_command(monkeypatch, [])

    result = CliRunner().invoke(cli_main, ["resources", "node-events"])

    assert result.exit_code != 0
