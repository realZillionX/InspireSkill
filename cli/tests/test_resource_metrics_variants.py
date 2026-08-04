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

from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api.metrics import MetricGroup, MetricSample

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
hpc_commands_module = importlib.import_module(
    "inspire.cli.commands.hpc.hpc_commands"
)
job_commands_module = importlib.import_module("inspire.cli.commands.job.job_commands")
serving_commands_module = importlib.import_module(
    "inspire.cli.commands.serving.serving_commands"
)
config_module = importlib.import_module("inspire.config")
web_session_module = importlib.import_module("inspire.platform.web.session")


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


def _patch_hpc_metrics_name_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
    )
    session = _FakeSession()

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=False: (config, {})),
    )
    monkeypatch.setattr(web_session_module, "get_web_session", lambda: session)

    def _fake_resolve_hpc_name_in_workspace(
        ctx,
        *,
        config,
        session,
        name,
        workspace,
        limit,
        pick=None,
        require_live=False,
    ):  # noqa: ANN001
        assert name == "prep-a"
        assert workspace == "Training Workspace"
        assert require_live is False
        return "hpc-job-xyz"

    monkeypatch.setattr(
        hpc_commands_module,
        "_resolve_hpc_name_in_workspace",
        _fake_resolve_hpc_name_in_workspace,
    )


def _patch_job_metrics_name_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
    )
    session = _FakeSession()
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=False: (config, {})),
    )
    monkeypatch.setattr(web_session_module, "get_web_session", lambda: session)

    def _fake_resolve_web_job_id(**kwargs: Any) -> str:
        assert kwargs["job"] == "train-job"
        assert kwargs["workspace"] == "Training Workspace"
        return "job-abc123"

    monkeypatch.setattr(job_commands_module, "_resolve_web_job_id", _fake_resolve_web_job_id)


def _patch_serving_metrics_name_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
    )
    session = _FakeSession()
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=False: (config, {})),
    )
    monkeypatch.setattr(web_session_module, "get_web_session", lambda: session)
    monkeypatch.setattr(
        serving_commands_module,
        "_resolve_workspace_id",
        lambda *_args, **_kwargs: "ws-fake",
    )

    def _fake_resolve_serving_name(ctx, name, *, workspace_id):  # noqa: ANN001
        assert name == "serving-a"
        assert workspace_id == "ws-fake"
        return "sv-abc"

    monkeypatch.setattr(
        serving_commands_module,
        "_resolve_serving_name",
        _fake_resolve_serving_name,
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
    _patch_job_metrics_name_resolver(monkeypatch)

    def _fake_request(session, method, path, *, referer=None, body=None, timeout=30):
        resolver_calls.append({"method": method, "path": path, "referer": referer, "body": body})
        return {"code": 0, "data": {"logic_compute_group_id": "lcg-train-42"}}

    monkeypatch.setattr(job_metrics_module, "_request_json", _fake_request)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "job",
            "metrics",
            "train-job",
            "--workspace",
            "Training Workspace",
            "--metric",
            "gpu",
            "--window",
            "30m",
        ],
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
    expected = tmp_path / "job-train-job-1000000.png"
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
    _patch_hpc_metrics_name_resolver(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "hpc",
            "metrics",
            "prep-a",
            "--workspace",
            "Training Workspace",
            "--metric",
            "gpu",
            "--window",
            "15m",
        ],
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
    expected = tmp_path / "hpc-prep-a-1000000.png"
    assert render_captures[0]["out_path"] == expected


def test_hpc_metrics_rejects_platform_handle_before_web_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_session():  # noqa: ANN001
        raise AssertionError("web session should not be opened for handle-shaped input")

    monkeypatch.setattr(web_session_module, "get_web_session", _fail_session)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "hpc", "metrics", "hpc-job-123", "--workspace", "all", "--metric", "gpu"],
    )

    assert result.exit_code != 0
    assert "ValidationError" in result.output
    assert "hpc name" in result.output


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
    _patch_serving_metrics_name_resolver(monkeypatch)

    class _FakeBrowserApi:
        @staticmethod
        def get_serving_detail(*, inference_serving_id: str, session):  # noqa: ANN001
            resolver_calls.append({"serving_id": inference_serving_id})
            return {"logic_compute_group_id": "lcg-serving-3"}

    monkeypatch.setattr(serving_metrics_module, "browser_api_module", _FakeBrowserApi)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "serving",
            "metrics",
            "serving-a",
            "--workspace",
            "Training Workspace",
            "--metric",
            "gpu",
            "--window",
            "10m",
        ],
    )
    assert result.exit_code == 0, result.output

    assert resolver_calls == [{"serving_id": "sv-abc"}]
    assert capture["task_type"] == "inference_serving"
    assert capture["logic_compute_group_id"] == "lcg-serving-3"
    assert render_captures[0]["task_label"] == "Serving"
    expected = tmp_path / "serving-serving-a-1000000.png"
    assert render_captures[0]["out_path"] == expected


def test_serving_metrics_retries_stale_cached_handle_by_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
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

    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
    )
    session = _FakeSession()
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=False: (config, {})),
    )
    monkeypatch.setattr(web_session_module, "get_web_session", lambda: session)
    monkeypatch.setattr(
        serving_commands_module,
        "_resolve_workspace_id",
        lambda *_args, **_kwargs: "ws-fake",
    )

    resolve_calls: list[bool] = []
    invalidated: list[str] = []

    def _resolve(
        _ctx,
        _name,
        *,
        workspace_id,
        pick=None,
        require_live=False,
    ):
        resolve_calls.append(require_live)
        return "sv-new" if require_live else "sv-old"

    monkeypatch.setattr(serving_commands_module, "_resolve_serving_name", _resolve)
    monkeypatch.setattr(
        serving_commands_module,
        "forget_resource_identity",
        lambda **kwargs: invalidated.append(kwargs["resource_id"]),
    )

    detail_calls: list[str] = []

    class _FakeBrowserApi:
        @staticmethod
        def get_serving_detail(*, inference_serving_id: str, session):  # noqa: ANN001
            detail_calls.append(inference_serving_id)
            if inference_serving_id == "sv-old":
                raise RuntimeError("not found")
            return {"logic_compute_group_id": "lcg-serving-fresh"}

    monkeypatch.setattr(serving_metrics_module, "browser_api_module", _FakeBrowserApi)

    result = CliRunner().invoke(
        cli_main,
        [
            "serving",
            "metrics",
            "serving-a",
            "--workspace",
            "Training Workspace",
            "--metric",
            "gpu",
            "--window",
            "10m",
        ],
    )

    assert result.exit_code == 0, result.output
    assert resolve_calls == [False, True]
    assert detail_calls == ["sv-old", "sv-new"]
    assert invalidated == ["sv-old"]
    assert capture["logic_compute_group_id"] == "lcg-serving-fresh"


# ---------------------------------------------------------------------------
# --json parity across variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resource,args",
    [
        (
            "job",
            [
                "job",
                "metrics",
                "train-job",
                "--workspace",
                "Training Workspace",
                "--metric",
                "gpu",
            ],
        ),
        (
            "hpc",
            [
                "hpc",
                "metrics",
                "prep-a",
                "--workspace",
                "Training Workspace",
                "--metric",
                "gpu",
            ],
        ),
        (
            "serving",
            [
                "serving",
                "metrics",
                "serving-a",
                "--workspace",
                "Training Workspace",
                "--metric",
                "gpu",
            ],
        ),
    ],
)
def test_variants_emit_resource_tagged_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    resource: str,
    args: list[str],
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
    if resource == "hpc":
        _patch_hpc_metrics_name_resolver(monkeypatch)
    elif resource == "job":
        _patch_job_metrics_name_resolver(monkeypatch)
    elif resource == "serving":
        _patch_serving_metrics_name_resolver(monkeypatch)

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
    assert "task_type" not in payload
    assert payload["name"] in {"train-job", "prep-a", "serving-a"}
    assert f"{resource}_id" not in payload
    # --json branch must skip PNG rendering.
    assert render_captures == []
