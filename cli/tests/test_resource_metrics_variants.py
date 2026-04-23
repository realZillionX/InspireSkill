"""Coverage for ``inspire job / hpc / serving metrics`` variants.

Each resource wrapper just contributes a ``lcg_resolver``; the rest of the
flow lives in the shared factory. These tests pin the wiring:

- the Browser-API detail call the resolver makes goes to the right path and
  body shape (train_job POST with ``job_id``; hpc_jobs REST-style GET;
  inference_servings GET via the existing helper)
- ``task_type`` forwarded to the metrics wrapper matches the backend enum
- default-plot filename uses the resource name (``job-…``, ``hpc-…``,
  ``serving-…``) so the same base dir disambiguates
- PNG title label is the human-readable form ("Train Job" / "HPC Job" /
  "Serving")
"""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest
from click.testing import CliRunner

metrics_shared = importlib.import_module("inspire.cli.utils.metrics_shared")
job_metrics_module = importlib.import_module(
    "inspire.cli.commands.job.job_metrics"
)
hpc_metrics_module = importlib.import_module(
    "inspire.cli.commands.hpc.hpc_metrics"
)
serving_metrics_module = importlib.import_module(
    "inspire.cli.commands.serving.serving_metrics"
)

from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api.metrics import MetricGroup, MetricSample


class _FakeSession:
    workspace_id = "ws-fake"


def _common_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    groups: list[MetricGroup],
    now: int,
    capture: dict,
    render_captures: list[dict],
    tmp_metrics_dir: str,
) -> None:
    session = _FakeSession()
    monkeypatch.setattr(metrics_shared, "get_web_session", lambda: session)

    def _fake_metrics_call(**kwargs: Any) -> list[MetricGroup]:
        capture.update(kwargs)
        return groups

    monkeypatch.setattr(metrics_shared, "get_resource_metrics_by_time", _fake_metrics_call)
    monkeypatch.setattr(metrics_shared.time, "time", lambda: now)

    def _fake_render(**kwargs: Any):
        render_captures.append(kwargs)
        return kwargs["out_path"]

    monkeypatch.setattr(metrics_shared, "render_metrics_png", _fake_render)
    monkeypatch.setenv("INSPIRE_METRICS_DIR", tmp_metrics_dir)


def _minimal_group() -> MetricGroup:
    return MetricGroup(
        group_name="pod-x",
        metric_type="gpu_usage_rate",
        resource_name="GPU",
        samples=[MetricSample(timestamp=100, value=0.5)],
    )


# ---------------------------------------------------------------------------
# Train job
# ---------------------------------------------------------------------------


def test_job_metrics_resolver_and_wiring(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    capture: dict = {}
    render_captures: list[dict] = []
    resolver_calls: list[dict] = []
    _common_monkeypatch(
        monkeypatch,
        groups=[_minimal_group()],
        now=1_000_000,
        capture=capture,
        render_captures=render_captures,
        tmp_metrics_dir=str(tmp_path),
    )

    def _fake_request(session, method, path, *, referer=None, body=None, timeout=30):
        resolver_calls.append({"method": method, "path": path, "referer": referer, "body": body})
        return {"code": 0, "data": {"logic_compute_group_id": "lcg-train-42"}}

    monkeypatch.setattr(job_metrics_module, "_request_json", _fake_request)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["job", "metrics", "job-abc123", "--metric", "gpu", "--window", "30m"],
    )
    assert result.exit_code == 0, result.output

    # Resolver hit the Browser API train_job/detail POST with the right body.
    assert len(resolver_calls) == 1
    call = resolver_calls[0]
    assert call["method"] == "POST"
    assert call["path"].endswith("/train_job/detail")
    assert call["body"] == {"job_id": "job-abc123"}
    assert "/jobs/distributedTrainingDetail/job-abc123" in call["referer"]

    # Factory forwarded the right task_type and resolved lcg.
    assert capture["task_type"] == "distributed_training"
    assert capture["logic_compute_group_id"] == "lcg-train-42"

    # Default path + PNG title label match the train-job resource identity.
    assert render_captures[0]["task_label"] == "Train Job"
    expected = tmp_path / "job-job-abc123-1000000.png"
    assert render_captures[0]["out_path"] == expected


# ---------------------------------------------------------------------------
# HPC
# ---------------------------------------------------------------------------


def test_hpc_metrics_resolver_and_wiring(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    capture: dict = {}
    render_captures: list[dict] = []
    resolver_calls: list[dict] = []
    _common_monkeypatch(
        monkeypatch,
        groups=[_minimal_group()],
        now=1_000_000,
        capture=capture,
        render_captures=render_captures,
        tmp_metrics_dir=str(tmp_path),
    )

    def _fake_request(session, method, path, *, referer=None, body=None, timeout=30):
        resolver_calls.append({"method": method, "path": path, "referer": referer, "body": body})
        return {"code": 0, "data": {"logic_compute_group_id": "lcg-hpc-9"}}

    monkeypatch.setattr(hpc_metrics_module, "_request_json", _fake_request)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["hpc", "metrics", "hpc-job-xyz", "--metric", "gpu", "--window", "15m"],
    )
    assert result.exit_code == 0, result.output

    call = resolver_calls[0]
    assert call["method"] == "GET"  # RESTful detail path
    assert call["path"].endswith("/hpc_jobs/hpc-job-xyz")
    assert call["body"] is None
    assert "/jobs/hpcDetail/hpc-job-xyz" in call["referer"]

    assert capture["task_type"] == "hpc_job"
    assert capture["logic_compute_group_id"] == "lcg-hpc-9"
    assert render_captures[0]["task_label"] == "HPC Job"
    expected = tmp_path / "hpc-hpc-job-xyz-1000000.png"
    assert render_captures[0]["out_path"] == expected


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


def test_serving_metrics_resolver_and_wiring(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    capture: dict = {}
    render_captures: list[dict] = []
    resolver_calls: list[dict] = []
    _common_monkeypatch(
        monkeypatch,
        groups=[_minimal_group()],
        now=1_000_000,
        capture=capture,
        render_captures=render_captures,
        tmp_metrics_dir=str(tmp_path),
    )

    class _FakeBrowserApi:
        @staticmethod
        def get_serving_detail(*, inference_serving_id: str, session):  # noqa: ANN001
            resolver_calls.append({"serving_id": inference_serving_id})
            return {"logic_compute_group_id": "lcg-serving-3"}

    monkeypatch.setattr(serving_metrics_module, "browser_api_module", _FakeBrowserApi)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["serving", "metrics", "sv-abc", "--metric", "gpu", "--window", "10m"],
    )
    assert result.exit_code == 0, result.output

    assert resolver_calls == [{"serving_id": "sv-abc"}]
    assert capture["task_type"] == "inference_serving"
    assert capture["logic_compute_group_id"] == "lcg-serving-3"
    assert render_captures[0]["task_label"] == "Serving"
    expected = tmp_path / "serving-sv-abc-1000000.png"
    assert render_captures[0]["out_path"] == expected


# ---------------------------------------------------------------------------
# --json parity across variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resource,args,task_type",
    [
        (
            "job",
            ["job", "metrics", "job-abc", "--metric", "gpu"],
            "distributed_training",
        ),
        (
            "hpc",
            ["hpc", "metrics", "hpc-job-xyz", "--metric", "gpu"],
            "hpc_job",
        ),
        (
            "serving",
            ["serving", "metrics", "sv-abc", "--metric", "gpu"],
            "inference_serving",
        ),
    ],
)
def test_variants_emit_resource_tagged_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    resource: str,
    args: list[str],
    task_type: str,
) -> None:
    capture: dict = {}
    render_captures: list[dict] = []
    _common_monkeypatch(
        monkeypatch,
        groups=[_minimal_group()],
        now=1_000_000,
        capture=capture,
        render_captures=render_captures,
        tmp_metrics_dir=str(tmp_path),
    )

    # Bypass each resource's resolver with a constant lcg so the test stays
    # focused on the --json envelope shape.
    monkeypatch.setattr(
        job_metrics_module, "_request_json",
        lambda *a, **kw: {"code": 0, "data": {"logic_compute_group_id": "lcg-ok"}},
    )
    monkeypatch.setattr(
        hpc_metrics_module, "_request_json",
        lambda *a, **kw: {"code": 0, "data": {"logic_compute_group_id": "lcg-ok"}},
    )

    class _FakeServingApi:
        @staticmethod
        def get_serving_detail(*, inference_serving_id, session):  # noqa: ANN001
            return {"logic_compute_group_id": "lcg-ok"}

    monkeypatch.setattr(serving_metrics_module, "browser_api_module", _FakeServingApi)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", *args])
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.output)
    payload = envelope["data"]
    assert payload["resource"] == resource
    assert payload["task_type"] == task_type
    # JSON id field matches the resource noun (e.g. `hpc_id` for hpc), giving
    # each variant a unique top-level identifier in mixed output.
    assert payload[f"{resource}_id"] == args[2]
    # --json branch must skip PNG rendering.
    assert render_captures == []
