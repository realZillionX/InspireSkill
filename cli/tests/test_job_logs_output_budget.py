from __future__ import annotations

import io
import json
import logging
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from inspire.bridge.tunnel import BridgeNotFoundError, TunnelError, TunnelNotAvailableError
from inspire.cli.commands.job import job_logs
from inspire.cli.main import main as cli_main


class _FakeSession:
    workspace_id = "ws-default"
    all_workspace_ids = ["ws-default"]
    all_workspace_names = {"ws-default": "Test Workspace"}
    storage_state = {"cookies": [{"name": "session", "value": "ok"}]}


def _patch_platform_resolution(monkeypatch: pytest.MonkeyPatch) -> _FakeSession:
    session = _FakeSession()
    monkeypatch.setattr(job_logs, "get_web_session", lambda: session)
    monkeypatch.setattr(job_logs, "_resolve_web_job_id", lambda **kwargs: "job-internal")
    monkeypatch.setattr(job_logs, "_close_web_client", lambda: None)
    monkeypatch.setattr(
        job_logs.browser_api_module,
        "get_job_detail_v2",
        lambda job_id, *, session: {"created_at": "1000"},
    )
    monkeypatch.setattr(
        job_logs.browser_api_module,
        "list_job_instances",
        lambda job_id, *, limit, session: ([{"name": "worker-name"}], 1),
    )
    return session


def _patch_ssh(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str,
) -> list[str]:
    commands: list[str] = []
    monkeypatch.setattr(job_logs, "_resolve_web_job_id", lambda **kwargs: "job-internal")
    monkeypatch.setattr(
        job_logs,
        "_resolve_tunnel_preflight_target",
        lambda bridge_name: (bridge_name, None, True),
    )
    monkeypatch.setattr(job_logs, "is_tunnel_available", lambda **kwargs: True)

    def fake_run_ssh_command(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        command = kwargs.get("command") or args[0]
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(job_logs, "run_ssh_command", fake_run_ssh_command)
    return commands


def _ssh_args(*extra: str) -> list[str]:
    return [
        "job",
        "logs",
        "train-a",
        "--workspace",
        "Test Workspace",
        "--source",
        "ssh",
        "--remote-log-path",
        "/logs/train.log",
        *extra,
    ]


def _platform_args(*extra: str) -> list[str]:
    return [
        "job",
        "logs",
        "train-a",
        "--workspace",
        "Test Workspace",
        "--source",
        "platform",
        *extra,
    ]


def test_help_documents_default_budgets_and_explicit_all() -> None:
    result = CliRunner().invoke(cli_main, ["job", "logs", "--help"])
    output = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "--all" in output
    assert "--tail INTEGER" in output
    assert "-n, --limit INTEGER" in output
    assert "without printing the remote path" in output
    assert f"Default one-shot output uses {job_logs.DEFAULT_SSH_TAIL_LINES}" in output
    assert f"{job_logs.DEFAULT_LOG_CHARACTER_LIMIT}-character limit" in output
    assert f"default: {job_logs.DEFAULT_PLATFORM_LOG_RECORDS}" in output


@pytest.mark.parametrize(
    ("extra", "conflict"),
    [
        (("--tail", "5"), "--tail"),
        (("--head", "5"), "--head"),
        (("--limit", "5"), "--limit"),
        (("--follow",), "--follow"),
        (("--path",), "--path"),
    ],
)
def test_all_rejects_bounded_or_streaming_modes(
    extra: tuple[str, ...],
    conflict: str,
) -> None:
    result = CliRunner().invoke(cli_main, _platform_args("--all", *extra))

    assert result.exit_code != 0
    assert "--all cannot be combined with" in result.output
    assert conflict in result.output


def test_tail_and_head_are_mutually_exclusive() -> None:
    result = CliRunner().invoke(
        cli_main,
        _platform_args("--tail", "5", "--head", "5"),
    )

    assert result.exit_code != 0
    assert "--tail and --head cannot be used together" in result.output


def test_ssh_default_uses_bounded_tail_and_json_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = "job-12345678-1234-1234-1234-123456789abc"
    stdout = "".join(
        f"line-{idx:03d} {'x' * 240} {raw_id}\n"
        for idx in range(job_logs.DEFAULT_SSH_TAIL_LINES + 1)
    )
    commands = _patch_ssh(monkeypatch, stdout=stdout)

    result = CliRunner().invoke(cli_main, ["--json", *_ssh_args()])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    data = payload["data"]
    assert commands == [f"tail -n {job_logs.DEFAULT_SSH_TAIL_LINES + 1} '/logs/train.log'"]
    assert data["truncated"] is True
    assert data["shown"] == len(data["content"].splitlines())
    assert data["total"] is None
    assert data["limit"] == job_logs.DEFAULT_SSH_TAIL_LINES
    assert data["character_limit"] == job_logs.DEFAULT_LOG_CHARACTER_LIMIT
    assert len(data["content"]) <= job_logs.DEFAULT_LOG_CHARACTER_LIMIT
    assert "log_path" not in data
    assert "/logs/train.log" not in result.output
    assert "line-100" in data["content"]
    assert raw_id not in result.output


def test_ssh_path_mode_reports_resolution_without_remote_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_path = "/inspire/private/project/training_master_train-a.log"
    _patch_ssh(monkeypatch, stdout="")
    args = [
        "job",
        "logs",
        "train-a",
        "--workspace",
        "Test Workspace",
        "--source",
        "ssh",
        "--remote-log-path",
        secret_path,
        "--path",
    ]

    human = CliRunner().invoke(cli_main, args)
    json_result = CliRunner().invoke(cli_main, ["--json", *args])

    assert human.exit_code == 0, human.output
    assert human.output == "Log location selected for train-a.\n"
    assert json_result.exit_code == 0, json_result.output
    assert json.loads(json_result.output)["data"] == {
        "name": "train-a",
        "status": "log-location-selected",
    }
    assert secret_path not in human.output
    assert secret_path not in json_result.output
    assert "/inspire/private" not in human.output
    assert "/inspire/private" not in json_result.output


def test_ssh_human_output_has_short_truncation_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = "".join(f"line-{idx:03d}\n" for idx in range(job_logs.DEFAULT_SSH_TAIL_LINES + 1))
    _patch_ssh(monkeypatch, stdout=stdout)

    result = CliRunner().invoke(cli_main, _ssh_args())

    assert result.exit_code == 0, result.output
    assert "line-000" not in result.output
    assert "line-100" in result.output
    assert "Logs truncated" in result.output
    assert "use --all" in result.output


def test_ssh_read_failure_hides_path_and_engineering_stderr(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_path = "/inspire/internal/project/private/train.log"
    detail = f"cat: {secret_path}: Permission denied"
    monkeypatch.setattr(
        job_logs,
        "run_ssh_command",
        lambda **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr=detail),
    )

    with caplog.at_level("DEBUG", logger=job_logs.__name__):
        with pytest.raises(IOError, match="Could not read the requested job log over SSH"):
            job_logs._fetch_log_via_ssh(secret_path, tail=10)

    assert secret_path in caplog.text
    assert detail in caplog.text


def test_ssh_requires_explicit_remote_log_path() -> None:
    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            "logs",
            "train-a",
            "--workspace",
            "Test Workspace",
            "--source",
            "ssh",
        ],
    )

    assert result.exit_code != 0
    assert "--source ssh requires --remote-log-path" in result.output


def test_ssh_all_uses_cat_and_bypasses_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = "job-12345678-1234-1234-1234-123456789abc"
    stdout = "".join(f"line-{idx:03d} {'x' * 180} {raw_id}\n" for idx in range(120))
    commands = _patch_ssh(monkeypatch, stdout=stdout)

    result = CliRunner().invoke(cli_main, ["--json", *_ssh_args("--all")])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert commands == ["cat '/logs/train.log'"]
    assert data["truncated"] is False
    assert data["shown"] == 120
    assert data["total"] == 120
    assert data["limit"] is None
    assert data["character_limit"] is None
    assert len(data["content"]) > job_logs.DEFAULT_LOG_CHARACTER_LIMIT
    assert raw_id not in result.output


def test_platform_default_applies_record_and_character_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_platform_resolution(monkeypatch)
    raw_id = "job-12345678-1234-1234-1234-123456789abc"
    calls: list[int] = []

    def fake_list_train_job_logs(**kwargs):  # noqa: ANN001, ANN202
        calls.append(kwargs["page_size"])
        return (
            [
                {
                    "timestamp_ms": str(idx),
                    "timestamp_str": f"t{idx}",
                    "log_id": f"log-{idx}",
                    "pod_name": raw_id,
                    "message": f"message-{idx} {'x' * 300} {raw_id}",
                }
                for idx in range(job_logs.DEFAULT_PLATFORM_LOG_RECORDS)
            ],
            250,
        )

    monkeypatch.setattr(
        job_logs.browser_api_module,
        "list_train_job_logs",
        fake_list_train_job_logs,
    )

    result = CliRunner().invoke(cli_main, ["--json", *_platform_args()])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert calls == [job_logs.DEFAULT_PLATFORM_LOG_RECORDS]
    assert data["truncated"] is True
    assert data["shown"] == len(data["logs"])
    assert data["shown"] <= job_logs.DEFAULT_PLATFORM_LOG_RECORDS
    assert data["total"] == 250
    assert data["limit"] == job_logs.DEFAULT_PLATFORM_LOG_RECORDS
    assert data["character_limit"] == job_logs.DEFAULT_LOG_CHARACTER_LIMIT
    assert data["shown_chars"] <= job_logs.DEFAULT_LOG_CHARACTER_LIMIT
    assert raw_id not in result.output


def test_platform_human_output_has_short_truncation_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_platform_resolution(monkeypatch)
    monkeypatch.setattr(
        job_logs.browser_api_module,
        "list_train_job_logs",
        lambda **kwargs: (
            [
                {
                    "timestamp_ms": str(idx),
                    "timestamp_str": f"t{idx}",
                    "pod_name": "worker-name",
                    "message": f"message-{idx}",
                }
                for idx in range(job_logs.DEFAULT_PLATFORM_LOG_RECORDS)
            ],
            200,
        ),
    )

    result = CliRunner().invoke(cli_main, _platform_args())

    assert result.exit_code == 0, result.output
    assert "Job Logs" in result.output
    assert "Logs truncated (showing 100 of 200 records)" in result.output
    assert "use --all" in result.output


def test_platform_all_refetches_reported_total_and_bypasses_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_platform_resolution(monkeypatch)
    calls: list[int] = []

    def fake_list_train_job_logs(**kwargs):  # noqa: ANN001, ANN202
        page_size = kwargs["page_size"]
        calls.append(page_size)
        count = min(page_size, 150)
        return (
            [
                {
                    "timestamp_ms": str(idx),
                    "timestamp_str": f"t{idx}",
                    "log_id": f"log-{idx}",
                    "pod_name": "worker-name",
                    "message": f"message-{idx}",
                }
                for idx in range(count)
            ],
            150,
        )

    monkeypatch.setattr(
        job_logs.browser_api_module,
        "list_train_job_logs",
        fake_list_train_job_logs,
    )

    result = CliRunner().invoke(cli_main, ["--json", *_platform_args("--all")])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert calls == [job_logs.DEFAULT_PLATFORM_LOG_RECORDS, 150]
    assert data["truncated"] is False
    assert data["shown"] == 150
    assert data["total"] == 150
    assert data["limit"] is None
    assert data["character_limit"] is None
    assert len(data["logs"]) == 150


def test_platform_follow_bounds_each_poll_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_platform_resolution(monkeypatch)
    raw_id = "job-12345678-1234-1234-1234-123456789abc"
    monkeypatch.setattr(
        job_logs.browser_api_module,
        "list_train_job_logs",
        lambda **kwargs: (
            [
                {
                    "timestamp_ms": "1000",
                    "timestamp_str": "t1",
                    "pod_name": raw_id,
                    "message": f"{'x' * (job_logs.DEFAULT_LOG_CHARACTER_LIMIT * 2)} {raw_id}",
                }
            ],
            1,
        ),
    )
    monkeypatch.setattr(
        job_logs.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(job_logs.time, "time", lambda: 10)

    result = CliRunner().invoke(cli_main, _platform_args("--follow"))

    assert result.exit_code == 0, result.output
    assert len(result.output) < job_logs.DEFAULT_LOG_CHARACTER_LIMIT + 300
    assert "Follow update truncated to the character budget" in result.output
    assert raw_id not in result.output


@pytest.mark.parametrize("error_type", [TunnelError, TunnelNotAvailableError, BridgeNotFoundError])
@pytest.mark.parametrize("remote_log_path", ["/logs/train.log", "/logs/train-*.log"])
def test_ssh_follow_tunnel_failure_exits_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[TunnelError],
    remote_log_path: str,
) -> None:
    _patch_platform_resolution(monkeypatch)
    _patch_ssh(monkeypatch, stdout="")
    monkeypatch.setattr(job_logs, "_find_connected_tunnel_bridges", lambda **kwargs: [])
    calls = []

    def fail_ssh(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append((args, kwargs))
        raise error_type("tunnel unavailable")

    monkeypatch.setattr(job_logs, "run_ssh_command", fail_ssh)
    sleeps = []
    def unexpected_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        pytest.fail("Tunnel failure must exit before sleeping")

    monkeypatch.setattr(job_logs.time, "sleep", unexpected_sleep)
    monkeypatch.setattr(job_logs.time, "time", lambda: 100.0)
    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level
    args = _ssh_args("--follow")
    args[args.index("--remote-log-path") + 1] = remote_log_path

    result = CliRunner().invoke(cli_main, args)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert "SSH tunnel not available" in result.output
    assert "Timeout:" not in result.output
    assert "Traceback" not in result.output
    assert len(calls) == 1
    assert sleeps == []
    assert api_logger.level == original_level


@pytest.mark.parametrize(
    "error",
    [
        TunnelError("tunnel failed"),
        TunnelNotAvailableError("unavailable"),
        BridgeNotFoundError("bridge missing"),
        subprocess.TimeoutExpired("ssh", 10),
        RuntimeError("probe failed"),
    ],
)
def test_latest_log_ssh_probe_preserves_tunnel_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    def fail_ssh(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise error

    monkeypatch.setattr(job_logs, "run_ssh_command", fail_ssh)
    caplog.set_level(logging.DEBUG, logger=job_logs.__name__)
    if isinstance(error, TunnelError):
        with pytest.raises(type(error)) as excinfo:
            job_logs._resolve_latest_log_via_ssh("/logs/train-*.log")
        assert excinfo.value is error
    else:
        assert job_logs._resolve_latest_log_via_ssh("/logs/train-*.log") is None
        assert any(
            record.message == "Latest log SSH probe failed" and record.exc_info
            for record in caplog.records
        )


@pytest.mark.parametrize("recover", [False, True])
def test_ssh_follow_status_failure_warning_is_bounded_and_resets(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    recover: bool,
) -> None:
    from inspire import process_io
    from inspire.bridge import tunnel

    _patch_ssh(monkeypatch, stdout="exists")
    monkeypatch.setattr(job_logs, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(tunnel, "get_ssh_command_args", lambda **kwargs: ["ssh"])
    monkeypatch.setattr(job_logs.time, "sleep", lambda seconds: None)
    now = [100.0]
    monkeypatch.setattr(job_logs.time, "time", lambda: now[0])
    stdout = io.StringIO()
    process = SimpleNamespace(stdout=stdout, poll=lambda: 0)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    statuses = ["error"] * 7 + (["RUNNING"] if recover else []) + ["error"] * 7 + ["SUCCEEDED"]
    observed = []

    def lines(*args):  # noqa: ANN002, ANN202
        for _ in statuses:
            now[0] += 5
            yield None

    def status(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        value = statuses[len(observed)]
        observed.append(value)
        if value == "error":
            raise RuntimeError("status unavailable")
        return {"status": value}

    monkeypatch.setattr(process_io, "iter_process_lines", lines)
    monkeypatch.setattr(job_logs.browser_api_module, "get_job_detail_v2", status)
    caplog.set_level(logging.DEBUG, logger=job_logs.__name__)
    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level

    assert job_logs._follow_logs_via_ssh("job-internal", "/logs/train.log") == "SUCCEEDED"

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == (2 if recover else 1)
    assert all("failed 5 consecutive times" in record.message for record in warnings)
    failures = [
        record
        for record in caplog.records
        if record.message == "Job status query failed while following logs"
    ]
    assert len(failures) == 14
    assert all(record.exc_info for record in failures)
    assert observed == statuses
    assert api_logger.level == original_level


@pytest.mark.parametrize("remote_log_path", ["/logs/train.log", "/logs/train-*.log"])
def test_ssh_follow_retries_transient_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    remote_log_path: str,
) -> None:
    monkeypatch.setattr(job_logs, "get_web_session", lambda: _FakeSession())
    calls = []
    sleeps = []

    def probe(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("ssh", 10)
        raise TunnelNotAvailableError("unavailable")

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        assert len(sleeps) == 1, "Tunnel errors must not trigger further retries"

    monkeypatch.setattr(job_logs, "run_ssh_command", probe)
    monkeypatch.setattr(job_logs.time, "sleep", sleep)
    monkeypatch.setattr(job_logs.time, "time", lambda: 100.0)
    caplog.set_level(logging.DEBUG, logger=job_logs.__name__)

    with pytest.raises(TunnelNotAvailableError):
        job_logs._follow_logs_via_ssh("job-internal", remote_log_path)

    assert len(calls) == 2
    assert sleeps == [5]
    assert any(
        record.levelno == logging.DEBUG
        and record.exc_info
        and isinstance(record.exc_info[1], subprocess.TimeoutExpired)
        for record in caplog.records
    )
