from __future__ import annotations

from click.testing import CliRunner

from inspire.cli.commands.job import job_logs
from inspire.cli.main import main as cli_main


class _FakeSession:
    workspace_id = "ws-default"
    storage_state = {"cookies": [{"name": "session", "value": "ok"}]}


def _patch_web_resolution(monkeypatch) -> _FakeSession:  # noqa: ANN001
    session = _FakeSession()
    monkeypatch.setattr(job_logs.Config, "from_files_and_env", lambda **kwargs: (object(), []))
    monkeypatch.setattr(job_logs, "get_web_session", lambda: session)
    monkeypatch.setattr(job_logs, "_resolve_web_job_id", lambda **kwargs: "job-abc")
    monkeypatch.setattr(job_logs, "_close_web_client", lambda: None)
    monkeypatch.setattr(
        job_logs.browser_api_module,
        "get_job_detail",
        lambda job_id, *, session: {"created_at": "1000"},
    )
    monkeypatch.setattr(
        job_logs.browser_api_module,
        "list_job_instances",
        lambda job_id, *, num, session: (
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
        ["job", "logs", "train-a", "--web", "--follow", "--tail", "1"],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert calls[0]["job_id"] == "job-abc"
    assert calls[0]["pod_names"] == ["worker-0"]
    assert "old" not in result.output
    assert "latest <job-id>" in result.output
    assert "new <job-id>" in result.output
    assert "job-12345678-1234-1234-1234-123456789abc" not in result.output
    assert "Stopped following logs." in result.output


def test_web_follow_accepts_explicit_instance_without_listing_instances(monkeypatch) -> None:  # noqa: ANN001
    _patch_web_resolution(monkeypatch)

    def fail_list_instances(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("explicit --instance should skip instance discovery")

    captured = {}

    def fake_list_train_job_logs(**kwargs):  # noqa: ANN001
        captured.update(kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(job_logs.browser_api_module, "list_job_instances", fail_list_instances)
    monkeypatch.setattr(job_logs.browser_api_module, "list_train_job_logs", fake_list_train_job_logs)
    monkeypatch.setattr(job_logs.time, "time", lambda: 10)

    result = CliRunner().invoke(
        cli_main,
        ["job", "logs", "train-a", "--web", "--follow", "--instance", "rank-0"],
    )

    assert result.exit_code == 0, result.output
    assert captured["pod_names"] == ["rank-0"]


def test_web_follow_json_is_rejected_before_web_calls(monkeypatch) -> None:  # noqa: ANN001
    def fail_config(**kwargs):  # noqa: ANN001
        raise AssertionError("json follow validation should run before web setup")

    monkeypatch.setattr(job_logs.Config, "from_files_and_env", fail_config)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "job", "logs", "train-a", "--web", "--follow"],
    )

    assert result.exit_code != 0
    assert "--json --follow --web is not supported" in result.output
