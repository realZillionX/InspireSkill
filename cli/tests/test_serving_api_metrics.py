"""Unit tests for `inspire serving api-metrics`.

`GetServingApiMetric` is a different metric family from the `GetTaskMetric`
that backs `serving metrics`: request traffic rather than resource
utilization, no shared metric name, no compute-group handle, and the whole
`metric_types` list honoured in one request instead of needing a per-metric
fan-out. These tests pin the selector, the request shape and the name-only
output so the two commands can't quietly converge.
"""

from __future__ import annotations

import json
from typing import Any

import click
import pytest
from click.testing import CliRunner

# `serving/__init__.py` re-exports the command under the module's own name, so
# the package attribute is the Click command, not the module.
from importlib import import_module

from inspire import config as config_module
from inspire.cli.commands.serving import serving_commands as serving_commands_module
from inspire.cli.main import main as cli_main
from inspire.platform.web import browser_api as browser_api_module

api_metrics_module = import_module(
    "inspire.cli.commands.serving.serving_api_metrics"
)


class _FakeSession:
    storage_state: dict[str, Any] = {}
    workspace_id = "ws-1"
    all_workspace_names = {"ws-1": "Serving空间"}
    all_workspace_ids = ["ws-1"]


def _patch_cli(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    config = config_module.Config(username="user", password="pass")
    resolutions: list[dict[str, Any]] = []

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(
        api_metrics_module, "get_web_session", lambda: _FakeSession()
    )
    monkeypatch.setattr(
        serving_commands_module,
        "_resolve_workspace_id",
        lambda _workspace, session=None: "ws-internal",
    )

    def _resolve(_ctx, name, *, workspace_id=None, pick=None, require_live=False):
        resolutions.append({"name": name, "pick": pick, "require_live": require_live})
        return "sv-internal"

    monkeypatch.setattr(serving_commands_module, "_resolve_serving_name", _resolve)
    return resolutions


def _install_metrics(monkeypatch: pytest.MonkeyPatch, groups: list[dict[str, Any]]) -> dict[str, Any]:
    calls: dict[str, Any] = {}

    def _fake(serving_id, *, metric_types, start_timestamp, end_timestamp, interval_second, session=None):
        calls["serving_id"] = serving_id
        calls["metric_types"] = list(metric_types)
        calls["window"] = end_timestamp - start_timestamp
        calls["interval_second"] = interval_second
        return groups

    monkeypatch.setattr(browser_api_module, "get_serving_api_metrics", _fake)
    return calls


_GROUPS = [
    {
        "metric_type": "QPS",
        "data_unit": "req/s",
        "time_series": [
            {"timestamp": "100", "data": 1.0},
            {"timestamp": "160", "data": 3.0},
        ],
    }
]


# ---------------------------------------------------------------------------
# Metric selection
# ---------------------------------------------------------------------------


def test_core_selection_is_the_traffic_triage_set() -> None:
    assert api_metrics_module._resolve_api_metrics(None) == [
        "QPS",
        "SUCCESS_RATE",
        "LATENCY",
    ]
    assert api_metrics_module._resolve_api_metrics("core") == [
        "QPS",
        "SUCCESS_RATE",
        "LATENCY",
    ]


def test_aliases_and_raw_names_both_resolve_and_deduplicate() -> None:
    assert api_metrics_module._resolve_api_metrics("ttft,TTFT,output_tokens") == [
        "TTFT",
        "OUTPUT_TOKENS",
    ]


def test_all_selection_covers_every_declared_metric() -> None:
    from inspire.platform.web.browser_api.servings import SERVING_API_METRIC_TYPES

    assert api_metrics_module._resolve_api_metrics("all") == list(
        SERVING_API_METRIC_TYPES
    )


def test_resource_metric_names_are_rejected_before_the_request() -> None:
    # `gpu_usage_rate` belongs to `serving metrics`; the two families share no
    # metric name and the wire would reject it.
    with pytest.raises(click.BadParameter, match="unknown serving API metric"):
        api_metrics_module._resolve_api_metrics("gpu_usage_rate")


@pytest.mark.parametrize("window", ["30m", "6h", "7d", "45s"])
def test_window_parsing_accepts_the_documented_suffixes(window: str) -> None:
    assert api_metrics_module._parse_window(window) > 0


@pytest.mark.parametrize("window", ["30", "1w", "", "h"])
def test_window_parsing_rejects_everything_else(window: str) -> None:
    with pytest.raises(click.BadParameter, match="unrecognized window"):
        api_metrics_module._parse_window(window)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def test_api_metrics_sends_the_whole_metric_list_in_one_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = _patch_cli(monkeypatch)
    calls = _install_metrics(monkeypatch, _GROUPS)

    result = CliRunner().invoke(
        cli_main,
        [
            "serving",
            "api-metrics",
            "demo-svc",
            "--workspace",
            "Serving空间",
            "--metric",
            "qps,latency",
            "--window",
            "30m",
            "--interval",
            "5m",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["serving_id"] == "sv-internal"
    assert calls["metric_types"] == ["QPS", "LATENCY"]
    assert calls["window"] == 1800
    assert calls["interval_second"] == 300
    assert resolutions[-1]["name"] == "demo-svc"


def test_api_metrics_json_is_a_compact_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch)
    _install_metrics(monkeypatch, _GROUPS)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "api-metrics",
            "demo-svc",
            "--workspace",
            "Serving空间",
            "--metric",
            "qps",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["resource"] == "serving"
    assert data["name"] == "demo-svc"
    assert data["metrics"] == ["QPS"]
    series = data["series"][0]
    assert series["metric"] == "QPS"
    assert series["unit"] == "req/s"
    assert series["count"] == 2
    assert series["min"] == 1.0
    assert series["max"] == 3.0
    assert series["avg"] == 2.0
    assert series["last"] == 3.0
    # Raw per-sample points would be an unbounded context cost for a question
    # the summary already answers.
    assert "samples" not in series


def test_api_metrics_reports_metrics_that_returned_no_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli(monkeypatch)
    _install_metrics(monkeypatch, _GROUPS)

    result = CliRunner().invoke(
        cli_main,
        [
            "serving",
            "api-metrics",
            "demo-svc",
            "--workspace",
            "Serving空间",
            "--metric",
            "qps,latency",
        ],
    )

    assert result.exit_code == 0, result.output
    # Silence about a requested metric reads as zero traffic; say it explicitly.
    assert "No data: LATENCY" in result.output


def test_api_metrics_says_so_when_nothing_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli(monkeypatch)
    _install_metrics(monkeypatch, [])

    result = CliRunner().invoke(
        cli_main,
        ["serving", "api-metrics", "demo-svc", "--workspace", "Serving空间"],
    )

    assert result.exit_code == 0, result.output
    assert "No API traffic reported in this window." in result.output


def test_api_metrics_rejects_a_platform_handle_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_api_module,
        "get_serving_api_metrics",
        lambda *_args, **_kwargs: pytest.fail(
            "raw handle must be rejected before the Browser API call"
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["serving", "api-metrics", "sv-12345678", "--workspace", "Serving空间"],
    )

    assert result.exit_code != 0
    assert "only accept serving names" in result.output


def test_api_metrics_bad_selector_fails_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "get_serving_api_metrics",
        lambda *_args, **_kwargs: pytest.fail("selector must be validated first"),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "serving",
            "api-metrics",
            "demo-svc",
            "--workspace",
            "Serving空间",
            "--metric",
            "nonsense",
        ],
    )

    assert result.exit_code != 0
    assert "unknown serving API metric" in result.output
