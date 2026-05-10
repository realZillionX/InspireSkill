from __future__ import annotations

import pytest
from click.testing import CliRunner

from inspire.cli.commands.job import job_commands
from inspire.cli.main import main as cli_main
from inspire.cli.utils import job_shell
from inspire.platform.web.browser_api import jobs as jobs_module


class _FakeSession:
    workspace_id = "ws-default"
    storage_state = {
        "cookies": [
            {"name": "inspire-session", "value": "cookie-v1", "domain": "qz.sii.edu.cn"}
        ]
    }
    cookies = None


def test_list_job_instances_uses_job_id_camel_case(monkeypatch) -> None:  # noqa: ANN001
    captured = {}

    def fake_request_json(session, method, path, *, referer, body=None, timeout=30):  # noqa: ANN001
        captured.update(
            {
                "session": session,
                "method": method,
                "path": path,
                "referer": referer,
                "body": body,
                "timeout": timeout,
            }
        )
        return {"code": 0, "data": {"items": [], "total": 0}}

    monkeypatch.setattr(jobs_module, "_request_json", fake_request_json)

    items, total = jobs_module.list_job_instances(
        "job-abc",
        num=7,
        session=_FakeSession(),
    )

    assert items == []
    assert total == 0
    assert captured["method"] == "POST"
    assert captured["path"].endswith("/train_job/instance_list")
    assert captured["body"] == {"jobId": "job-abc", "page_num": 1, "page_size": 7}


def test_build_remote_cmd_url_and_headers(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(job_shell, "_get_base_url", lambda: "https://qz.sii.edu.cn")
    monkeypatch.setattr(job_shell, "_browser_api_path", lambda path: f"/api/v1{path}")

    url = job_shell.build_remote_cmd_ws_url("job-abc", "worker-0")
    headers = job_shell.build_remote_cmd_headers(_FakeSession())

    assert url == (
        "wss://qz.sii.edu.cn/api/v1/train_job/remote_cmd?"
        "job_id=job-abc&instance_name=worker-0"
    )
    assert headers["Origin"] == "https://qz.sii.edu.cn"
    assert headers["Cookie"] == "inspire-session=cookie-v1"


def test_select_job_instance_requires_selector_for_multiple_running() -> None:
    instances = job_shell.normalize_job_instances(
        [
            {"name": "worker-0", "instance_status": "instance_running"},
            {"name": "worker-1", "instance_status": "instance_running"},
            {"name": "worker-2", "instance_status": "instance_failed"},
        ]
    )

    with pytest.raises(job_shell.JobShellError, match="Multiple running instances"):
        job_shell.select_job_instance(instances)

    assert job_shell.select_job_instance(instances, rank=0).name == "worker-0"
    assert job_shell.select_job_instance(instances, instance_name="worker-1").name == "worker-1"


def test_select_job_instance_prompts_for_multiple_running(monkeypatch) -> None:  # noqa: ANN001
    instances = job_shell.normalize_job_instances(
        [
            {"name": "worker-0", "instance_status": "instance_running"},
            {"name": "worker-1", "instance_status": "instance_running"},
        ]
    )

    monkeypatch.setattr(job_shell.click, "prompt", lambda *args, **kwargs: 2)

    assert job_shell.select_job_instance(instances, prompt=True).name == "worker-1"


def test_open_job_shell_retries_once_after_401(monkeypatch) -> None:  # noqa: ANN001
    calls = []
    refreshed = _FakeSession()

    def fake_run_remote_shell(*, session, **kwargs):  # noqa: ANN001
        del kwargs
        calls.append(session)
        if len(calls) == 1:
            raise job_shell.JobShellAuthError("401")
        return 0

    monkeypatch.setattr(job_shell, "run_remote_shell", fake_run_remote_shell)
    monkeypatch.setattr(job_shell, "get_web_session", lambda force_refresh=False: refreshed)

    assert job_shell.open_job_shell(
        job_id="job-abc",
        instance_name="worker-0",
        session=_FakeSession(),
    ) == 0
    assert len(calls) == 2
    assert calls[1] is refreshed


def test_job_shell_command_uses_web_resolver_and_rank_selector(monkeypatch) -> None:  # noqa: ANN001
    captured = {}

    monkeypatch.setattr(
        job_commands,
        "resolve_job_id",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not use slow resolver")),
    )
    monkeypatch.setattr(job_commands, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(job_commands.Config, "from_files_and_env", lambda **kwargs: (object(), []))

    def fake_resolve_web_job_id(**kwargs):  # noqa: ANN001
        captured["resolve"] = kwargs
        return "job-abc"

    monkeypatch.setattr(job_commands, "_resolve_web_job_id", fake_resolve_web_job_id)

    def fake_list_job_instances(job_id, *, num, session):  # noqa: ANN001
        captured["list"] = {
            "job_id": job_id,
            "num": num,
            "session": session,
        }
        return (
            [
                {"name": "worker-0", "instance_status": "instance_running"},
                {"name": "worker-1", "instance_status": "instance_running"},
            ],
            2,
        )

    def fake_open_job_shell(*, job_id, instance_name, session):  # noqa: ANN001
        captured["shell"] = {
            "job_id": job_id,
            "instance_name": instance_name,
            "session": session,
        }
        return 0

    monkeypatch.setattr(job_commands.browser_api_module, "list_job_instances", fake_list_job_instances)
    monkeypatch.setattr(job_commands, "open_job_shell", fake_open_job_shell)
    monkeypatch.setattr(job_commands, "_close_web_client", lambda: None)

    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            "shell",
            "train-a",
                "--pick",
                "2",
                "--rank",
                "1",
                "-A",
                "--max-pages",
                "7",
            ],
    )

    assert result.exit_code == 0, result.output
    assert captured["resolve"]["job"] == "train-a"
    assert captured["resolve"]["all_workspaces"] is True
    assert captured["resolve"]["max_pages"] == 7
    assert captured["resolve"]["pick"] == 2
    assert captured["list"]["job_id"] == "job-abc"
    assert captured["shell"]["job_id"] == "job-abc"
    assert captured["shell"]["instance_name"] == "worker-1"
    assert "Press Ctrl-]" in result.output


def test_job_shell_command_prompts_for_multiple_instances(monkeypatch) -> None:  # noqa: ANN001
    captured = {}

    monkeypatch.setattr(job_commands.Config, "from_files_and_env", lambda **kwargs: (object(), []))
    monkeypatch.setattr(job_commands, "_resolve_web_job_id", lambda **kwargs: "job-abc")
    monkeypatch.setattr(job_commands, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(
        job_commands.browser_api_module,
        "list_job_instances",
        lambda *args, **kwargs: (
            [
                {"name": "worker-0", "instance_status": "instance_running"},
                {"name": "worker-1", "instance_status": "instance_running"},
            ],
            2,
        ),
    )
    monkeypatch.setattr(
        job_commands,
        "open_job_shell",
        lambda **kwargs: captured.update(kwargs) or 0,
    )
    monkeypatch.setattr(job_commands, "_close_web_client", lambda: None)

    result = CliRunner().invoke(cli_main, ["job", "shell", "train-a"], input="2\n")

    assert result.exit_code == 0, result.output
    assert captured["instance_name"] == "worker-1"
    assert "Select instance" in result.output


def test_job_shell_command_rejects_multiple_selectors() -> None:
    result = CliRunner().invoke(
        cli_main,
        ["job", "shell", "train-a", "--rank", "0", "--instance", "worker-0"],
    )

    assert result.exit_code != 0
    assert "Use only one of --rank or --instance" in result.output


def test_job_instances_requires_workspace_and_uses_num(monkeypatch) -> None:  # noqa: ANN001
    captured = {}

    monkeypatch.setattr(job_commands.Config, "from_files_and_env", lambda **kwargs: (object(), []))
    monkeypatch.setattr(job_commands, "get_web_session", lambda: _FakeSession())

    def fake_resolve_web_job_id(**kwargs):  # noqa: ANN001
        captured["resolve"] = kwargs
        return "job-abc"

    def fake_list_job_instances(job_id, *, num, session):  # noqa: ANN001
        captured["list"] = {"job_id": job_id, "num": num, "session": session}
        return (
            [
                {
                    "name": "worker-0",
                    "instance_status": "instance_running",
                    "instance_type": "worker",
                    "node": "node-a",
                    "created_at": 0,
                }
            ],
            1,
        )

    monkeypatch.setattr(job_commands, "_resolve_web_job_id", fake_resolve_web_job_id)
    monkeypatch.setattr(job_commands.browser_api_module, "list_job_instances", fake_list_job_instances)
    monkeypatch.setattr(job_commands, "_close_web_client", lambda: None)

    missing_workspace = CliRunner().invoke(cli_main, ["job", "instances", "train-a"])
    assert missing_workspace.exit_code != 0
    assert "Missing option '--workspace'" in missing_workspace.output

    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            "instances",
                "train-a",
                "--workspace",
                "分布式训练空间",
                "--num",
                "42",
            ],
    )

    assert result.exit_code == 0, result.output
    assert captured["resolve"]["job"] == "train-a"
    assert captured["resolve"]["workspace"] == "分布式训练空间"
    assert captured["resolve"]["all_workspaces"] is False
    assert captured["resolve"]["scan_num"] == 42
    assert captured["list"]["job_id"] == "job-abc"
    assert captured["list"]["num"] == 42
    assert "worker-0" in result.output


def test_resolve_web_job_id_pick_selects_matching_job(monkeypatch) -> None:  # noqa: ANN001
    rows = [
        {"name": "train-a", "job_id": "job-1"},
        {"name": "train-a", "job_id": "job-2"},
    ]
    captured = {}

    def fake_list_web_jobs(**kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return rows, []

    monkeypatch.setattr(job_commands, "_list_web_jobs", fake_list_web_jobs)

    job_id = job_commands._resolve_web_job_id(
        config=object(),
        job="train-a",
        workspace=None,
        all_workspaces=True,
        max_pages=50,
        pick=2,
    )

    assert job_id == "job-2"
    assert captured["limit"] == 0


def test_job_shell_command_rejects_job_id_boundary(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(job_commands.Config, "from_files_and_env", lambda **kwargs: (object(), []))
    monkeypatch.setattr(
        job_commands,
        "_list_web_jobs",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("should not resolve platform handles")
        ),
    )

    result = CliRunner().invoke(cli_main, ["job", "shell", "job-abc"])

    assert result.exit_code != 0
    assert "take a job name" in result.output


class _FakeSocket:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    def recv(self, size: int) -> bytes:
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
            return chunk[:size]
        return chunk


def test_websocket_http_response_preserves_extra_frame_bytes() -> None:
    sock = _FakeSocket([b"HTTP/1.1 101 Switching Protocols\r\nHeader: value\r\n\r\n\x82\x05hello"])

    response, extra = job_shell._WebSocketClient._read_http_response(sock)

    assert response == "HTTP/1.1 101 Switching Protocols\r\nHeader: value\r\n\r\n"
    assert extra == b"\x82\x05hello"


def test_websocket_recv_exact_consumes_buffer_before_socket() -> None:
    sock = _FakeSocket([b"cd"])
    client = job_shell._WebSocketClient("wss://example.invalid", {})
    client.sock = sock
    client._recv_buffer = b"ab"

    assert client._recv_exact(3) == b"abc"
    assert client._recv_buffer == b""
