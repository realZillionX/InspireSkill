"""Tests for the ``inspire notebook metrics`` CLI command."""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest
from click.testing import CliRunner

metrics_module = importlib.import_module(
    "inspire.cli.commands.notebook.notebook_metrics"
)
from inspire.cli.context import EXIT_CONFIG_ERROR, EXIT_VALIDATION_ERROR
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api.metrics import (
    MetricGroup,
    MetricSample,
)


class _FakeSession:
    def __init__(self) -> None:
        self.workspace_id = "ws-fake"


def _install_common_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    detail: dict,
    groups: list[MetricGroup],
    now: int = 1_000_000,
    capture: dict | None = None,
    render_captures: list[dict] | None = None,
    tmp_metrics_dir: str | None = None,
) -> None:
    """Stub get_web_session, notebook detail, metrics wrapper, and PNG renderer.

    The renderer is always stubbed — no matplotlib writes to disk in tests;
    ``render_captures`` (if provided) receives the kwargs the command passed
    to ``render_metrics_png``.
    """
    session = _FakeSession()
    monkeypatch.setattr(metrics_module, "get_web_session", lambda: session)

    class _FakeBrowserApi:
        @staticmethod
        def get_notebook_detail(*, notebook_id: str, session):  # noqa: ANN001
            return detail

    monkeypatch.setattr(metrics_module, "browser_api_module", _FakeBrowserApi)

    def _fake_metrics_call(**kwargs: Any) -> list[MetricGroup]:
        if capture is not None:
            capture.update(kwargs)
        return groups

    monkeypatch.setattr(metrics_module, "get_resource_metrics_by_time", _fake_metrics_call)
    monkeypatch.setattr(metrics_module.time, "time", lambda: now)

    def _fake_render(**kwargs: Any):
        if render_captures is not None:
            render_captures.append(kwargs)
        return kwargs["out_path"]

    monkeypatch.setattr(metrics_module, "render_metrics_png", _fake_render)

    if tmp_metrics_dir is not None:
        monkeypatch.setenv("INSPIRE_METRICS_DIR", tmp_metrics_dir)


def _sample_groups() -> list[MetricGroup]:
    return [
        MetricGroup(
            group_name="pod-1",
            metric_type="gpu_usage_rate",
            resource_name="GPU",
            samples=[MetricSample(timestamp=t, value=v) for t, v in [(100, 0.1), (160, 0.8), (220, 0.5)]],
        ),
        MetricGroup(
            group_name="pod-1",
            metric_type="cpu_usage_rate",
            resource_name="CPU",
            samples=[MetricSample(timestamp=100, value=0.02)],
        ),
    ]


def test_metrics_json_output_is_raw_time_series_and_skips_plot(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    capture: dict = {}
    render_captures: list[dict] = []
    _install_common_fakes(
        monkeypatch,
        detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
        groups=_sample_groups(),
        now=1_000_000,
        capture=capture,
        render_captures=render_captures,
        tmp_metrics_dir=str(tmp_path),
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--json", "notebook", "metrics", "nb-xyz", "--metric", "gpu,cpu", "--window", "30m"],
    )

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["success"] is True
    payload = envelope["data"]
    assert payload["notebook_id"] == "nb-xyz"
    assert payload["logic_compute_group_id"] == "lcg-abc"
    assert payload["metric_types"] == ["gpu_usage_rate", "cpu_usage_rate"]
    assert payload["time_range"]["interval_second"] == 60
    assert payload["time_range"]["end_timestamp"] == 1_000_000
    assert payload["time_range"]["start_timestamp"] == 1_000_000 - 30 * 60
    assert len(payload["groups"]) == 2
    assert payload["groups"][0]["time_series"][1] == {"timestamp": 160, "value": 0.8}

    # The wrapper got called with the right arguments.
    assert capture["task_type"] == "interactive_modeling"
    assert capture["logic_compute_group_id"] == "lcg-abc"
    assert capture["metric_types"] == ["gpu_usage_rate", "cpu_usage_rate"]
    assert capture["interval_second"] == 60

    # --json must skip PNG rendering entirely.
    assert render_captures == []


def test_metrics_default_output_writes_png_and_prints_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    render_captures: list[dict] = []
    _install_common_fakes(
        monkeypatch,
        detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
        groups=_sample_groups(),
        now=1_000_000,
        render_captures=render_captures,
        tmp_metrics_dir=str(tmp_path),
    )
    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "metrics", "nb-xyz", "--metric", "gpu"])

    assert result.exit_code == 0, result.output

    # Default behavior: PNG path + text stats, no sparkline block chars.
    assert len(render_captures) == 1
    out_path = render_captures[0]["out_path"]
    expected = tmp_path / "nb-xyz-1000000.png"
    assert out_path == expected
    assert f"Chart: {expected}" in result.output

    # Text summary stays (percent scaling from ratio works).
    assert "gpu_usage_rate" in result.output
    assert "min=10.0%" in result.output
    assert "max=80.0%" in result.output
    # No sparkline unless explicitly requested.
    assert not any(ch in result.output for ch in "▁▂▃▄▅▆▇█")


def test_metrics_no_plot_suppresses_render(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    render_captures: list[dict] = []
    _install_common_fakes(
        monkeypatch,
        detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
        groups=_sample_groups(),
        render_captures=render_captures,
        tmp_metrics_dir=str(tmp_path),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["notebook", "metrics", "nb-xyz", "--metric", "gpu", "--no-plot"]
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
        detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
        groups=_sample_groups(),
        tmp_metrics_dir=str(tmp_path),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["notebook", "metrics", "nb-xyz", "--metric", "gpu", "--sparkline"]
    )
    assert result.exit_code == 0, result.output
    assert any(ch in result.output for ch in "▁▂▃▄▅▆▇█")


def test_metrics_custom_plot_path_is_honored(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    render_captures: list[dict] = []
    _install_common_fakes(
        monkeypatch,
        detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
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
            "nb-xyz",
            "--metric",
            "gpu",
            "--plot",
            str(custom),
        ],
    )
    assert result.exit_code == 0, result.output
    assert render_captures[0]["out_path"] == custom
    assert f"Chart: {custom}" in result.output


def test_metrics_rejects_unknown_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_common_fakes(
        monkeypatch,
        detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
        groups=[],
    )
    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["notebook", "metrics", "nb-xyz", "--metric", "throughput"]
    )
    assert result.exit_code == EXIT_VALIDATION_ERROR
    assert "unknown metric" in result.output


def test_metrics_errors_when_lcg_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_common_fakes(
        monkeypatch,
        detail={"start_config": {"logic_compute_group_id": ""}, "logic_compute_group": {}},
        groups=[],
    )
    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "metrics", "nb-xyz"])
    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "logic_compute_group_id" in result.output


def test_metrics_cli_honors_explicit_lcg(monkeypatch: pytest.MonkeyPatch) -> None:
    capture: dict = {}
    _install_common_fakes(
        monkeypatch,
        detail={"start_config": {"logic_compute_group_id": "lcg-ignored"}},
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
            "nb-xyz",
            "--lcg",
            "lcg-explicit",
            "--metric",
            "gpu",
        ],
    )
    assert result.exit_code == 0, result.output
    assert capture["logic_compute_group_id"] == "lcg-explicit"


def test_metrics_absolute_window(monkeypatch: pytest.MonkeyPatch) -> None:
    capture: dict = {}
    _install_common_fakes(
        monkeypatch,
        detail={"start_config": {"logic_compute_group_id": "lcg-abc"}},
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
            "nb-xyz",
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
    # 2026-04-22T23:34:37Z → 1 745 105 677 is not right... compute via datetime:
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
