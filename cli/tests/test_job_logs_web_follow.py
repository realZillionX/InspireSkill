from __future__ import annotations

from click.testing import CliRunner

from inspire.cli.commands.job import job_logs
from inspire.cli.main import main as cli_main


class _FakeSession:
    workspace_id = "ws-default"
    all_workspace_ids = ["ws-default"]
    all_workspace_names = {"ws-default": "Test Workspace"}
    storage_state = {"cookies": [{"name": "session", "value": "ok"}]}


def _patch_web_resolution(monkeypatch) -> _FakeSession:  # noqa: ANN001
    session = _FakeSession()
    monkeypatch.setattr(job_logs, "get_web_session", lambda: session)
    monkeypatch.setattr(job_logs, "_resolve_web_job_id", lambda **kwargs: "job-abc")
    monkeypatch.setattr(job_logs, "_close_web_client", lambda: None)
    monkeypatch.setattr(
        job_logs.browser_api_module,
        "get_job_detail_v2",
        lambda job_id, *, session: {"created_at": "1000"},
    )
    monkeypatch.setattr(
        job_logs.browser_api_module,
        "list_job_instances",
        lambda job_id, *, limit, session: (
            [{"name": "worker-0"}],
            1,
        ),
    )
    return session


def test_web_follow_polls_new_logs_and_scrubs_human_output(monkeypatch) -> None:  # noqa: ANN001
    _patch_web_resolution(monkeypatch)
    calls = []

    def fake_list_train_job_logs(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        raw_id = "job-12345678-1234-1234-1234-123456789abc"
        if len(calls) == 1:
            return (
                [
                    {
                        "timestamp_ms": "1000",
                        "timestamp_str": "t1",
                        "pod_name": raw_id,
                        "message": f"old {raw_id}",
                    },
                    {
                        "timestamp_ms": "2000",
                        "timestamp_str": "t2",
                        "pod_name": raw_id,
                        "message": f"latest {raw_id}",
                    },
                ],
                2,
            )
        return (
            [
                {
                    "timestamp_ms": "1000",
                    "timestamp_str": "t1",
                    "pod_name": raw_id,
                    "message": f"old {raw_id}",
                },
                {
                    "timestamp_ms": "2000",
                    "timestamp_str": "t2",
                    "pod_name": raw_id,
                    "message": f"latest {raw_id}",
                },
                {
                    "timestamp_ms": "3000",
                    "timestamp_str": "t3",
                    "pod_name": raw_id,
                    "message": f"new {raw_id}",
                },
            ],
            3,
        )

    sleep_calls = 0

    def fake_sleep(_seconds):  # noqa: ANN001
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise KeyboardInterrupt()

    monkeypatch.setattr(job_logs.browser_api_module, "list_train_job_logs", fake_list_train_job_logs)
    monkeypatch.setattr(job_logs.time, "sleep", fake_sleep)
    monkeypatch.setattr(job_logs.time, "time", lambda: 10)

    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            "logs",
            "train-a",
            "--workspace",
            "Test Workspace",
            "--source",
            "platform",
            "--follow",
            "--tail",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert calls[0]["job_id"] == "job-abc"
    assert calls[0]["pod_names"] == ["worker-0"]
    assert "old" not in result.output
    assert "latest <redacted>" in result.output
    assert "new <redacted>" in result.output
    assert "job-12345678-1234-1234-1234-123456789abc" not in result.output


def test_web_follow_resolves_an_explicit_instance_to_its_pod(monkeypatch) -> None:  # noqa: ANN001
    """`--instance` names the Rank; only the instance list knows its pod."""
    _patch_web_resolution(monkeypatch)

    captured = {}

    def fake_list_train_job_logs(**kwargs):  # noqa: ANN001
        captured.update(kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        job_logs.browser_api_module,
        "list_job_instances",
        lambda _job_id, **_kwargs: (
            [
                {"name": f"job-abc-worker-{rank}-0", "rank": rank}
                for rank in range(2)
            ],
            2,
        ),
    )
    monkeypatch.setattr(job_logs.browser_api_module, "list_train_job_logs", fake_list_train_job_logs)
    monkeypatch.setattr(job_logs.time, "time", lambda: 10)

    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            "logs",
            "train-a",
            "--workspace",
            "Test Workspace",
            "--source",
            "platform",
            "--follow",
            "--instance",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["pod_names"] == ["job-abc-worker-1-0"]


def test_web_logs_reject_an_unknown_instance(monkeypatch) -> None:  # noqa: ANN001
    _patch_web_resolution(monkeypatch)
    monkeypatch.setattr(
        job_logs.browser_api_module,
        "list_job_instances",
        lambda _job_id, **_kwargs: ([{"name": "job-abc-worker-0-0", "rank": 0}], 1),
    )
    monkeypatch.setattr(
        job_logs.browser_api_module,
        "list_train_job_logs",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("an unknown instance must not reach the log store")
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            "logs",
            "train-a",
            "--workspace",
            "Test Workspace",
            "--source",
            "platform",
            "--instance",
            "rank=7",
        ],
    )

    assert result.exit_code != 0
    assert "rank=0" in result.output


def test_web_follow_stops_once_the_job_is_terminal(monkeypatch) -> None:  # noqa: ANN001
    """Following a finished job used to poll forever; only the SSH path ever ended."""
    _patch_web_resolution(monkeypatch)
    monkeypatch.setattr(
        job_logs.browser_api_module,
        "get_job_detail_v2",
        lambda job_id, *, session: {"created_at": "1000", "status": "job_stopped"},
    )

    calls = []

    def fake_list_train_job_logs(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return ([{"timestamp_ms": "1000", "timestamp_str": "t1", "pod_name": "worker-0",
                  "message": "line"}], 1)

    # Every read advances the clock past the status-check interval, so the very
    # first poll is followed by a status read.
    ticks = {"now": 0.0}

    def fake_time() -> float:
        ticks["now"] += 60.0
        return ticks["now"]

    monkeypatch.setattr(job_logs.browser_api_module, "list_train_job_logs", fake_list_train_job_logs)
    monkeypatch.setattr(job_logs.time, "time", fake_time)
    monkeypatch.setattr(job_logs.time, "sleep", lambda _s: None)

    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            "logs",
            "train-a",
            "--workspace",
            "Test Workspace",
            "--source",
            "platform",
            "--follow",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Job reached job_stopped" in result.output
    # The first poll prints the tail, the drain poll catches trailing records.
    assert len(calls) == 2


def test_web_follow_json_is_rejected_before_web_calls(monkeypatch) -> None:  # noqa: ANN001
    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "logs",
            "train-a",
            "--workspace",
            "Test Workspace",
            "--source",
            "platform",
            "--follow",
        ],
    )

    assert result.exit_code != 0
    assert "--json --follow --source platform is not supported" in result.output


def test_job_logs_rejects_instance_handle_before_web_calls(monkeypatch) -> None:  # noqa: ANN001
    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            "logs",
            "train-a",
            "--workspace",
            "Test Workspace",
            "--source",
            "platform",
            "--instance",
            "pod-1234abcd",
        ],
    )

    assert result.exit_code != 0
    assert "job instance name" in result.output
    assert "pod-1234abcd" not in result.output
