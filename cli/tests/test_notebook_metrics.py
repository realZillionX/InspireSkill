"""Tests for the ``inspire notebook metrics`` CLI command + shared core.

The shared flow lives in :mod:`inspire.cli.utils.metrics_shared`; these tests
patch it there so the same fakes cover every resource-specific wrapper. See
``test_resource_metrics_variants.py`` for the job / hpc / serving checks.
"""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from inspire.cli.context import EXIT_CONFIG_ERROR, EXIT_VALIDATION_ERROR
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api.metrics import MetricGroup, MetricSample

metrics_shared = importlib.import_module("inspire.cli.utils.metrics_shared")
metrics_plot = importlib.import_module("inspire.cli.utils.metrics_plot")
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


class _FakeSession:
    def __init__(self) -> None:
        self.workspace_id = "ws-fake"
        self.all_workspace_ids = ["ws-fake"]
        self.all_workspace_names = {"ws-fake": "Fake Workspace"}


def _install_common_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    notebook_detail: dict,
    groups: list[MetricGroup],
    now: int = 1_000_000,
    capture: dict | None = None,
    render_captures: list[dict] | None = None,
    tmp_metrics_dir: str | None = None,
) -> None:
    """Stub the shared core + notebook-specific detail resolver.

    The renderer is always stubbed — no matplotlib writes to disk.
    ``render_captures`` (if provided) receives the kwargs the command
    passed to ``render_metrics_png``.
    """
    session = _FakeSession()
    monkeypatch.setattr(metrics_shared, "get_web_session", lambda: session)
    monkeypatch.setattr(notebook_cli_module, "require_web_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(notebook_cli_module, "load_config", lambda _ctx: SimpleNamespace(workspaces={}))
    monkeypatch.setattr(notebook_cli_module, "get_base_url", lambda: "https://example.test")
    monkeypatch.setattr(
        notebook_lookup_module,
        "_resolve_notebook_id",
        lambda *args, **kwargs: (_NOTEBOOK_ID, None),
    )

    class _FakeBrowserApi:
        @staticmethod
        def get_notebook_detail(*, notebook_id: str, session):  # noqa: ANN001
            return notebook_detail

    monkeypatch.setattr(notebook_metrics_module, "browser_api_module", _FakeBrowserApi)

    def _fake_metrics_call(**kwargs: Any) -> list[MetricGroup]:
        if capture is not None:
            capture.update(kwargs)
        return groups

    monkeypatch.setattr(metrics_shared, "get_resource_metrics_by_time", _fake_metrics_call)
    monkeypatch.setattr(metrics_shared.time, "time", lambda: now)

    def _fake_render(**kwargs: Any):
        if render_captures is not None:
            render_captures.append(kwargs)
        return kwargs["out_path"]

    monkeypatch.setattr(metrics_shared, "render_metrics_png", _fake_render)

    if tmp_metrics_dir is not None:
        monkeypatch.setenv("INSPIRE_METRICS_DIR", tmp_metrics_dir)


def _sample_groups() -> list[MetricGroup]:
    return [
        MetricGroup(
            group_name="pod-1",
            metric_type="gpu_usage_rate",
            resource_name="GPU",
            samples=[
                MetricSample(timestamp=t, value=v) for t, v in [(100, 0.1), (160, 0.8), (220, 0.5)]
            ],
        ),
        MetricGroup(
            group_name="pod-1",
            metric_type="cpu_usage_rate",
            resource_name="CPU",
            samples=[MetricSample(timestamp=100, value=0.02)],
        ),
    ]


def test_notebook_metrics_name_resolution_validates_cached_handle_with_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    ctx = SimpleNamespace(workspace="Fake Workspace", json_output=False)
    seen: dict[str, object] = {}
    detail_calls: list[str] = []

    monkeypatch.setattr(
        notebook_cli_module,
        "require_web_session",
        lambda *_args, **_kwargs: session,
    )
    monkeypatch.setattr(notebook_cli_module, "load_config", lambda _ctx: SimpleNamespace())
    monkeypatch.setattr(notebook_cli_module, "get_base_url", lambda: "https://example.test")
    monkeypatch.setattr(
        workspace_module,
        "resolve_workspace_query_scope",
        lambda *_args, **_kwargs: (["ws-live"], "ws-live"),
    )

    def fake_retry(*_args, operation, **kwargs):  # noqa: ANN001
        seen.update(kwargs)
        return operation("notebook-live"), "notebook-live", "ws-live"

    monkeypatch.setattr(
        notebook_lookup_module,
        "_run_notebook_operation_with_stale_handle_retry",
        fake_retry,
    )
    monkeypatch.setattr(
        notebook_metrics_module.browser_api_module,
        "get_notebook_detail",
        lambda *, notebook_id, session: detail_calls.append(notebook_id)
        or {"name": _NOTEBOOK_NAME},
    )

    target = notebook_metrics_module._notebook_name_to_id(ctx, _NOTEBOOK_NAME)

    assert target.task_id == "notebook-live"
    assert target.logic_compute_group_id is None
    assert seen["identifier"] == _NOTEBOOK_NAME
    assert seen["workspace_ids"] == ["ws-live"]
    assert detail_calls == ["notebook-live"]


def test_metrics_json_output_is_compact_name_only_summary_and_skips_plot(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    capture: dict = {}
    render_captures: list[dict] = []
    _install_common_fakes(
        monkeypatch,
        notebook_detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
        groups=_sample_groups(),
        now=1_000_000,
        capture=capture,
        render_captures=render_captures,
        tmp_metrics_dir=str(tmp_path),
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--json",
            "notebook",
            "metrics",
            _NOTEBOOK_NAME,
                "--workspace",
                "all",
            "--metric",
            "gpu,cpu",
            "--window",
            "30m",
        ],
    )

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["success"] is True
    payload = envelope["data"]
    assert payload["resource"] == "notebook"
    assert payload["name"] == _NOTEBOOK_NAME
    assert payload["metrics"] == ["gpu_usage_rate", "cpu_usage_rate"]
    assert "notebook_id" not in payload
    assert "logic_compute_group_id" not in payload
    assert "task_type" not in payload
    assert "metric_types" not in payload
    assert "groups" not in payload
    assert "time_series" not in payload
    assert payload["time_range"] == {
        "start": 1_000_000 - 30 * 60,
        "end": 1_000_000,
        "interval": "1m",
    }
    assert payload["series"][0] == {
        "unit": "pod-1",
        "metric": "gpu_usage_rate",
        "count": 3,
        "min": 0.1,
        "max": 0.8,
        "avg": pytest.approx((0.1 + 0.8 + 0.5) / 3),
        "last": 0.5,
    }
    assert payload["series"][1] == {
        "unit": "pod-1",
        "metric": "cpu_usage_rate",
        "count": 1,
        "min": 0.02,
        "max": 0.02,
        "avg": 0.02,
        "last": 0.02,
    }
    assert all("samples" not in series for series in payload["series"])

    assert capture["task_type"] == "interactive_modeling"
    assert capture["logic_compute_group_id"] == "lcg-abc"
    assert capture["metric_types"] == ["gpu_usage_rate", "cpu_usage_rate"]
    assert capture["interval_second"] == 60

    assert render_captures == []


def test_metrics_default_output_writes_png_and_prints_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    render_captures: list[dict] = []
    _install_common_fakes(
        monkeypatch,
        notebook_detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
        groups=_sample_groups(),
        now=1_000_000,
        render_captures=render_captures,
        tmp_metrics_dir=str(tmp_path),
    )
    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "metrics", _NOTEBOOK_NAME, "--workspace", "all", "--metric", "gpu"])

    assert result.exit_code == 0, result.output

    assert len(render_captures) == 1
    out_path = render_captures[0]["out_path"]
    # Default path now includes the resource name to disambiguate the four CLI
    # entry points that share the same base dir.
    expected = tmp_path / "notebook-demo-notebook-1000000.png"
    assert out_path == expected
    assert "Chart saved." in result.output
    assert str(expected) not in result.output
    assert render_captures[0]["task_label"] == "Notebook"
    assert render_captures[0]["task_id"] == _NOTEBOOK_NAME

    assert "gpu_usage_rate" in result.output
    assert "min=10.0%" in result.output
    assert "max=80.0%" in result.output
    assert not any(ch in result.output for ch in "▁▂▃▄▅▆▇█")


def test_metrics_no_plot_suppresses_render(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    render_captures: list[dict] = []
    _install_common_fakes(
        monkeypatch,
        notebook_detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
        groups=_sample_groups(),
        render_captures=render_captures,
        tmp_metrics_dir=str(tmp_path),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["notebook", "metrics", _NOTEBOOK_NAME, "--workspace", "all", "--metric", "gpu", "--no-plot"]
    )
    assert result.exit_code == 0, result.output
    assert render_captures == []
    assert "Chart:" not in result.output
    assert "gpu_usage_rate" in result.output


def test_metrics_sparkline_flag_includes_block_chars(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _install_common_fakes(
        monkeypatch,
        notebook_detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
        groups=_sample_groups(),
        tmp_metrics_dir=str(tmp_path),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["notebook", "metrics", _NOTEBOOK_NAME, "--workspace", "all", "--metric", "gpu", "--sparkline"]
    )
    assert result.exit_code == 0, result.output
    assert any(ch in result.output for ch in "▁▂▃▄▅▆▇█")


def test_metrics_custom_plot_path_is_honored(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    render_captures: list[dict] = []
    _install_common_fakes(
        monkeypatch,
        notebook_detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
        groups=_sample_groups(),
        render_captures=render_captures,
        tmp_metrics_dir=str(tmp_path),
    )
    runner = CliRunner()
    custom = tmp_path / "override" / "chart.png"
    result = runner.invoke(
        cli_main,
        [
            "notebook",
            "metrics",
            _NOTEBOOK_NAME,
                "--workspace",
                "all",
            "--metric",
            "gpu",
            "--plot",
            str(custom),
        ],
    )
    assert result.exit_code == 0, result.output
    assert render_captures[0]["out_path"] == custom
    assert f"Chart: {custom}" in result.output


def test_metrics_json_raw_is_bounded_and_reports_truncation(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _install_common_fakes(
        monkeypatch,
        notebook_detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
        groups=_sample_groups(),
        tmp_metrics_dir=str(tmp_path),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--json",
            "notebook",
            "metrics",
            _NOTEBOOK_NAME,
            "--workspace",
            "all",
            "--metric",
            "gpu",
            "--raw",
            "--raw-limit",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    series = json.loads(result.output)["data"]["series"][0]
    assert series["sample_mode"] == "raw"
    assert series["count"] == 3
    assert series["total"] == 3
    assert series["returned"] == 2
    assert series["truncated"] is True
    assert series["samples"] == [
        {"timestamp": 160, "value": 0.8},
        {"timestamp": 220, "value": 0.5},
    ]


def test_metrics_raw_default_limit_is_hard_bounded() -> None:
    group = MetricGroup(
        group_name="pod-1",
        metric_type="gpu_usage_rate",
        resource_name="GPU",
        samples=[MetricSample(timestamp=i, value=0.5) for i in range(2_001)],
    )

    series = metrics_shared._raw_series(
        group,
        limit=metrics_shared.DEFAULT_RAW_SAMPLE_LIMIT,
    )

    assert series["total"] == 2_001
    assert series["returned"] == metrics_shared.DEFAULT_RAW_SAMPLE_LIMIT
    assert len(series["samples"]) == metrics_shared.DEFAULT_RAW_SAMPLE_LIMIT
    assert series["truncated"] is True
    assert series["samples"][0]["timestamp"] == 1_501
    assert series["samples"][-1]["timestamp"] == 2_000


def test_metrics_help_documents_raw_mode_and_limit() -> None:
    result = CliRunner().invoke(cli_main, ["notebook", "metrics", "--help"])

    assert result.exit_code == 0, result.output
    assert "--raw" in result.output
    assert "--raw-limit" in result.output
    assert "hard limit" in result.output


def test_metrics_rejects_unknown_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_common_fakes(
        monkeypatch,
        notebook_detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
        groups=[],
    )
    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["notebook", "metrics", _NOTEBOOK_NAME, "--workspace", "all", "--metric", "throughput"]
    )
    assert result.exit_code == EXIT_VALIDATION_ERROR
    assert "unknown metric" in result.output


def test_metrics_errors_when_lcg_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_common_fakes(
        monkeypatch,
        notebook_detail={"start_config": {"logic_compute_group_id": ""}, "logic_compute_group": {}},
        groups=[],
    )
    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "metrics", _NOTEBOOK_NAME, "--workspace", "all"])
    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "Unable to resolve compute group" in result.output
    assert "logic_compute_group_id" not in result.output


def test_metrics_cli_resolves_explicit_group_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture: dict = {}
    _install_common_fakes(
        monkeypatch,
        notebook_detail={"start_config": {"logic_compute_group_id": "lcg-ignored"}},
        groups=[],
        capture=capture,
    )
    group_calls: dict[str, str] = {}

    def _resolve_group(ctx, *, session, workspace, name):  # noqa: ANN001
        del ctx, session
        group_calls["workspace"] = workspace
        group_calls["name"] = name
        return "lcg-explicit"

    monkeypatch.setattr(metrics_shared, "_resolve_compute_group_name", _resolve_group)
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--json",
            "notebook",
            "metrics",
            _NOTEBOOK_NAME,
            "--workspace",
            "all",
            "--group",
            "H200-2号机房",
            "--metric",
            "gpu",
        ],
    )
    assert result.exit_code == 0, result.output
    assert group_calls == {"workspace": "all", "name": "H200-2号机房"}
    assert capture["logic_compute_group_id"] == "lcg-explicit"


def test_metrics_absolute_window(monkeypatch: pytest.MonkeyPatch) -> None:
    capture: dict = {}
    _install_common_fakes(
        monkeypatch,
        notebook_detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
        groups=[],
        capture=capture,
    )
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--json",
            "notebook",
            "metrics",
            _NOTEBOOK_NAME,
                "--workspace",
                "all",
            "--metric",
            "gpu",
            "--start",
            "2026-04-22 23:34:37",
            "--end",
            "2026-04-23 01:38:20",
            "--interval",
            "5m",
        ],
    )
    assert result.exit_code == 0, result.output
    from datetime import datetime, timezone

    expected_start = int(
        datetime(2026, 4, 22, 23, 34, 37, tzinfo=timezone.utc).timestamp()
    )
    expected_end = int(
        datetime(2026, 4, 23, 1, 38, 20, tzinfo=timezone.utc).timestamp()
    )
    assert capture["start_timestamp"] == expected_start
    assert capture["end_timestamp"] == expected_end
    assert capture["interval_second"] == 300


# ---------------------------------------------------------------------------
# Multi-pod rendering / text summary
# ---------------------------------------------------------------------------


def _multi_worker_groups() -> list[MetricGroup]:
    """Mirror the 8-worker distributed-training shape (stragglers surfaced)."""
    values = [
        ("worker-0", 0.95),
        ("worker-1", 0.93),
        ("worker-2", 0.96),
        ("worker-3", 0.05),  # straggler — spread should pop
        ("worker-4", 0.94),
        ("worker-5", 0.92),
        ("worker-6", 0.95),
        ("worker-7", 0.97),
    ]
    groups = []
    for name, last in values:
        groups.append(
            MetricGroup(
                group_name=f"job-abc-{name}",
                metric_type="gpu_usage_rate",
                resource_name="GPU",
                samples=[
                    MetricSample(timestamp=100, value=last * 0.9),
                    MetricSample(timestamp=160, value=last),
                ],
            )
        )
    return groups


def test_multi_pod_text_summary_surfaces_stragglers(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    render_captures: list[dict] = []
    _install_common_fakes(
        monkeypatch,
        notebook_detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
        groups=_multi_worker_groups(),
        render_captures=render_captures,
        tmp_metrics_dir=str(tmp_path),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["notebook", "metrics", _NOTEBOOK_NAME, "--workspace", "all", "--metric", "gpu"]
    )
    assert result.exit_code == 0, result.output
    # Pod count reflected.
    assert "pods=8" in result.output
    # Spread / stragglers block shows up, pointing at the slow worker.
    assert "last-min=5.0% (worker-3)" in result.output
    assert "last-max=97.0% (worker-7)" in result.output
    assert "spread=92.0%" in result.output
    # Renderer received all 8 groups to draw.
    assert render_captures
    assert len(render_captures[0]["groups"]) == 8
