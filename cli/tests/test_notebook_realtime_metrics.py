"""Tests for ``notebook.GetRealtimeNotebookMetric`` and ``notebook metrics --now``.

``--now`` shares a Click command with the time-series path, so the dispatch
itself is covered here: adding the flag must not change what the default
invocation asks the platform for.
"""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from inspire.cli.context import EXIT_API_ERROR
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api import notebooks as notebooks_module
from inspire.platform.web.browser_api.notebooks import (
    NotebookResourceSnapshot,
    get_notebook_realtime_metrics,
)

metrics_shared = importlib.import_module("inspire.cli.utils.metrics_shared")
notebook_metrics_module = importlib.import_module(
    "inspire.cli.commands.notebook.notebook_metrics"
)
notebook_cli_module = importlib.import_module("inspire.cli.utils.notebook_cli")
notebook_lookup_module = importlib.import_module(
    "inspire.cli.commands.notebook.notebook_lookup"
)
workspace_module = importlib.import_module("inspire.config.workspaces")

_NOTEBOOK_NAME = "demo-notebook"
_NOTEBOOK_ID = "nb-xyz"

# Shape measured on qz.sii.edu.cn: four rows, `usage_rate` as a 0-1 ratio,
# `unit` populated only for Memory.
_LIVE_ROWS = [
    {
        "resource_name": "CPU",
        "total": 15,
        "used": 0.06,
        "available": 14.94,
        "usage_rate": 0.0042,
        "unit": "",
        "spec": "",
    },
    {
        "resource_name": "Memory",
        "total": 500,
        "used": 0.4,
        "available": 499.6,
        "usage_rate": 0.0008,
        "unit": "GB",
        "spec": "",
    },
    {
        "resource_name": "GPU",
        "total": 0,
        "used": 0,
        "available": 0,
        "usage_rate": 0,
        "unit": "",
        "spec": "",
    },
    {
        "resource_name": "GPU_Memory",
        "total": 0,
        "used": 0,
        "available": 0,
        "usage_rate": 0,
        "unit": "",
        "spec": "",
    },
]


class _FakeSession:
    def __init__(self) -> None:
        self.workspace_id = "ws-fake"
        self.all_workspace_ids = ["ws-fake"]
        self.all_workspace_names = {"ws-fake": "Fake Workspace"}


def _json_data(output: str) -> Any:
    parsed = json.loads(output)
    return parsed.get("data", parsed)


# ---------------------------------------------------------------------------
# Wrapper
# ---------------------------------------------------------------------------


def test_realtime_wrapper_projects_the_live_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_notebook_v2(session, action, body=None, *, timeout=30):  # noqa: ANN001
        captured["action"] = action
        captured["body"] = body
        return {"resource_metric_list": _LIVE_ROWS}

    monkeypatch.setattr(notebooks_module, "_notebook_v2", fake_notebook_v2)
    monkeypatch.setattr(notebooks_module, "get_web_session", lambda: _FakeSession())

    rows = get_notebook_realtime_metrics(notebook_id="nb-1")

    assert captured["action"] == "GetRealtimeNotebookMetric"
    assert captured["body"] == {"notebook_id": "nb-1"}
    assert rows[0] == NotebookResourceSnapshot(
        resource="CPU",
        total=15.0,
        used=0.06,
        available=14.94,
        usage_rate=0.0042,
        unit="",
    )
    assert [row.resource for row in rows] == ["CPU", "Memory", "GPU", "GPU_Memory"]
    assert rows[1].unit == "GB"


def test_realtime_wrapper_refuses_an_empty_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sent with no notebook_id the gateway answers cluster-wide totals, which
    # would read as one notebook holding thousands of GPUs.
    calls: list[str] = []

    def fake_notebook_v2(session, action, body=None, *, timeout=30):  # noqa: ANN001
        calls.append(action)
        return {"resource_metric_list": []}

    monkeypatch.setattr(notebooks_module, "_notebook_v2", fake_notebook_v2)

    with pytest.raises(ValueError, match="notebook handle"):
        get_notebook_realtime_metrics(notebook_id="")
    assert calls == []


def test_realtime_wrapper_tolerates_a_missing_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        notebooks_module,
        "_notebook_v2",
        lambda session, action, body=None, *, timeout=30: {},
    )
    monkeypatch.setattr(notebooks_module, "get_web_session", lambda: _FakeSession())

    assert get_notebook_realtime_metrics(notebook_id="nb-1") == []


def test_realtime_wrapper_propagates_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(session, action, body=None, *, timeout=30):  # noqa: ANN001
        raise ValueError("API error: ResourceNotFound: notebook not found")

    monkeypatch.setattr(notebooks_module, "_notebook_v2", _raise)
    monkeypatch.setattr(notebooks_module, "get_web_session", lambda: _FakeSession())

    with pytest.raises(ValueError, match="ResourceNotFound"):
        get_notebook_realtime_metrics(notebook_id="nb-1")


# ---------------------------------------------------------------------------
# `notebook metrics --now`
# ---------------------------------------------------------------------------


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str = "RUNNING",
    rows: list[NotebookResourceSnapshot] | None = None,
    realtime_error: Exception | None = None,
) -> dict[str, Any]:
    session = _FakeSession()
    monkeypatch.setattr(metrics_shared, "get_web_session", lambda: session)
    monkeypatch.setattr(
        notebook_cli_module, "require_web_session", lambda *args, **kwargs: session
    )
    monkeypatch.setattr(
        notebook_cli_module, "load_config", lambda _ctx: SimpleNamespace(workspaces={})
    )
    monkeypatch.setattr(notebook_cli_module, "get_base_url", lambda: "https://example.test")
    monkeypatch.setattr(
        workspace_module,
        "resolve_workspace_operation_scope",
        lambda *_args, **_kwargs: "ws-fake",
    )
    monkeypatch.setattr(
        notebook_lookup_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: (_NOTEBOOK_ID, None),
    )

    captured: dict[str, Any] = {}

    class _FakeBrowserApi:
        @staticmethod
        def get_notebook_detail(*, notebook_id: str, session):  # noqa: ANN001
            return {
                "status": status,
                "start_config": {"logic_compute_group_id": "lcg-1"},
            }

        @staticmethod
        def get_notebook_realtime_metrics(*, notebook_id: str, session):  # noqa: ANN001
            captured["notebook_id"] = notebook_id
            if realtime_error is not None:
                raise realtime_error
            return rows if rows is not None else _snapshot_rows()

    monkeypatch.setattr(notebook_metrics_module, "browser_api_module", _FakeBrowserApi)
    return captured


def _snapshot_rows() -> list[NotebookResourceSnapshot]:
    return [
        NotebookResourceSnapshot(
            resource=str(row["resource_name"]),
            total=float(row["total"]),
            used=float(row["used"]),
            available=float(row["available"]),
            usage_rate=float(row["usage_rate"]),
            unit=str(row["unit"]),
        )
        for row in _LIVE_ROWS
    ]


def _zeroed_rows() -> list[NotebookResourceSnapshot]:
    return [
        NotebookResourceSnapshot(
            resource=row.resource,
            total=0.0,
            used=0.0,
            available=0.0,
            usage_rate=0.0,
            unit=row.unit,
        )
        for row in _snapshot_rows()
    ]


def _argv(*extra: str) -> list[str]:
    return [
        "notebook",
        "metrics",
        _NOTEBOOK_NAME,
        "--workspace",
        "Fake Workspace",
        *extra,
    ]


def test_now_renders_the_current_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_fakes(monkeypatch)

    result = CliRunner().invoke(cli_main, _argv("--now"))

    assert result.exit_code == 0
    assert captured["notebook_id"] == _NOTEBOOK_ID
    assert f"Notebook Metrics — {_NOTEBOOK_NAME} (now)" in result.output
    assert "Status: RUNNING" in result.output
    # 0-1 ratios are rendered as percentages, matching the history output.
    assert "0.4%" in result.output
    assert "0.40 GB" in result.output
    # A notebook with no card must not read as 0% GPU utilization.
    assert "0.0%" not in result.output
    assert _NOTEBOOK_ID not in result.output


def test_now_says_a_stopped_notebook_is_not_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fakes(monkeypatch, status="STOPPED", rows=_zeroed_rows())

    result = CliRunner().invoke(cli_main, _argv("--now"))

    assert result.exit_code == 0
    assert "Status: STOPPED" in result.output
    assert "Not running" in result.output


def test_now_json_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch)

    result = CliRunner().invoke(cli_main, ["--json", *_argv("--now")])

    assert result.exit_code == 0
    payload = _json_data(result.output)
    assert payload["resource"] == "notebook"
    assert payload["name"] == _NOTEBOOK_NAME
    assert payload["mode"] == "realtime"
    assert payload["status"] == "RUNNING"
    assert payload["usage"][0] == {
        "resource": "CPU",
        "used": 0.06,
        "total": 15.0,
        "available": 14.94,
        "usage_rate": 0.0042,
        "unit": "",
    }
    assert _NOTEBOOK_ID not in result.output


def test_now_surfaces_a_failed_call_instead_of_zeros(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fakes(
        monkeypatch,
        realtime_error=ValueError("API error: ResourceNotFound: notebook not found"),
    )

    result = CliRunner().invoke(cli_main, _argv("--now"))

    assert result.exit_code == EXIT_API_ERROR
    assert "Status:" not in result.output


def test_without_now_the_command_still_queries_the_time_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fakes(monkeypatch)
    captured: dict[str, Any] = {}

    def _fake_metrics_call(**kwargs: Any) -> list:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(metrics_shared, "get_resource_metrics_by_time", _fake_metrics_call)

    result = CliRunner().invoke(cli_main, _argv("--no-plot", "--window", "30m"))

    assert result.exit_code == 0
    assert captured["task_id"] == _NOTEBOOK_ID
    assert captured["task_type"] == "interactive_modeling"
    assert captured["end_timestamp"] - captured["start_timestamp"] == 1800
    assert "(now)" not in result.output
