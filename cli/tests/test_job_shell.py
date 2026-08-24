from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from inspire.cli.commands.hpc import hpc_commands
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


def test_list_job_instances_uses_v2_action_api(monkeypatch) -> None:  # noqa: ANN001
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
        return {"ResponseMetadata": {"Action": "ListJobInstances"}, "Result": {"items": [], "total": 0}}

    monkeypatch.setattr(jobs_module, "_request_json", fake_request_json)

    items, total = jobs_module.list_job_instances(
        "job-abc",
        limit=7,
        session=_FakeSession(),
    )

    assert items == []
    assert total == 0
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v2/train?Action=ListJobInstances"
    assert captured["body"] == {"job_id": "job-abc", "page_num": 1, "page_size": 7}


def test_build_remote_cmd_url_and_headers(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(job_shell, "_get_base_url", lambda: "https://qz.sii.edu.cn")

    url = job_shell.build_remote_cmd_ws_url("job-abc", "worker-0")
    headers = job_shell.build_remote_cmd_headers(_FakeSession())

    # v2, and no `?Action=`: the PTY sockets are the REST-shaped half of the
    # gateway, which is why an Action-name inventory kept reporting this one
    # as having no v2 counterpart.
    assert url == (
        "wss://qz.sii.edu.cn/api/v2/train_job/remote_cmd?"
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

    monkeypatch.setattr(job_commands, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(job_commands.Config, "from_files_and_env", lambda **kwargs: (object(), []))

    def fake_resolve_web_job_id(**kwargs):  # noqa: ANN001
        captured["resolve"] = kwargs
        return "job-abc"

    monkeypatch.setattr(job_commands, "_resolve_web_job_id", fake_resolve_web_job_id)

    def fake_list_job_instances(job_id, *, limit, session):  # noqa: ANN001
        captured["list"] = {
            "job_id": job_id,
            "limit": limit,
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
            "--workspace",
            "Test Workspace",
            "--pick",
            "2",
            "--rank",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["resolve"]["job"] == "train-a"
    assert captured["resolve"]["workspace"] == "Test Workspace"
    assert captured["resolve"]["all_workspaces"] is False
    assert captured["resolve"]["max_pages"] == 50
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

    result = CliRunner().invoke(
        cli_main,
        ["job", "shell", "train-a", "--workspace", "Test Workspace"],
        input="2\n",
    )

    assert result.exit_code == 0, result.output
    assert captured["instance_name"] == "worker-1"
    assert "Select instance" in result.output


def test_hpc_shell_command_passes_live_session_to_instance_fetch(monkeypatch) -> None:  # noqa: ANN001
    captured = {}
    session = _FakeSession()

    monkeypatch.setattr(hpc_commands, "get_web_session", lambda: session)
    monkeypatch.setattr(
        hpc_commands,
        "_resolve_hpc_name_in_workspace",
        lambda *args, **kwargs: "hpc-abc",
    )

    def fake_fetch(job_id, *, limit, session, show_all):  # noqa: ANN001
        captured["fetch"] = {
            "job_id": job_id,
            "limit": limit,
            "session": session,
            "show_all": show_all,
        }
        return (
            [
                {
                    "name": "project/hpc-abc-cluster-launcher-0",
                    "status": "Running",
                    "component": "launcher",
                }
            ],
            1,
        )

    monkeypatch.setattr(hpc_commands, "_fetch_hpc_instances", fake_fetch)
    monkeypatch.setattr(
        hpc_commands,
        "open_job_shell",
        lambda **kwargs: captured.update({"shell": kwargs}) or 0,
    )

    result = CliRunner().invoke(
        cli_main,
        ["hpc", "shell", "prep-a", "--workspace", "CPU资源空间"],
    )

    assert result.exit_code == 0, result.output
    assert captured["fetch"] == {
        "job_id": "hpc-abc",
        "limit": 200,
        "session": session,
        "show_all": True,
    }
    assert captured["shell"] == {
        "job_id": "hpc-abc",
        "instance_name": "project/hpc-abc-cluster-launcher-0",
        "session": session,
        "workload": "hpc",
    }
    assert "Opening shell: prep-a / launcher" in result.output


def test_job_shell_command_rejects_multiple_selectors() -> None:
    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            "shell",
            "train-a",
            "--workspace",
            "Test Workspace",
            "--rank",
            "0",
            "--instance",
            "worker-0",
        ],
    )

    assert result.exit_code != 0
    assert "Use only one of --rank or --instance" in result.output


@pytest.mark.parametrize(
    "instance_name",
    [
        "550e8400-e29b-41d4-a716-446655440000",
        "pod-1234abcd",
        "instance-1234abcd",
    ],
)
def test_job_shell_rejects_instance_handles_before_api(
    monkeypatch, instance_name: str
) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        job_commands.Config,
        "from_files_and_env",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("instance validation should run before config")
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            "shell",
            "train-a",
            "--workspace",
            "Test Workspace",
            "--instance",
            instance_name,
        ],
    )

    assert result.exit_code != 0
    assert "job instance name" in result.output
    assert instance_name not in result.output


def test_job_instances_requires_workspace_and_uses_limit(monkeypatch) -> None:  # noqa: ANN001
    captured = {}

    monkeypatch.setattr(job_commands.Config, "from_files_and_env", lambda **kwargs: (object(), []))
    monkeypatch.setattr(job_commands, "get_web_session", lambda: _FakeSession())

    def fake_resolve_web_job_id(**kwargs):  # noqa: ANN001
        captured["resolve"] = kwargs
        return "job-abc"

    def fake_list_job_instances(job_id, *, limit, session):  # noqa: ANN001
        captured["list"] = {"job_id": job_id, "limit": limit, "session": session}
        return (
            [
                {
                    "instance_id": "job-abc-worker-deadbeef",
                    "name": "worker-0",
                    "instance_status": "instance_running",
                    "instance_type": "worker",
                    "node": "node-a",
                    "created_at": 0,
                    "resource_spec": {
                        "cpu_count": 4,
                        "memory_size_gib": 32,
                        "gpu_count": 1,
                    },
                    "backend": "browser",
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
            "--limit",
            "42",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["resolve"]["job"] == "train-a"
    assert captured["resolve"]["workspace"] == "分布式训练空间"
    assert captured["resolve"]["all_workspaces"] is False
    assert captured["resolve"]["scan_limit"] == 42
    assert captured["list"]["job_id"] == "job-abc"
    assert captured["list"]["limit"] == 42
    assert result.output.splitlines()[0].lstrip().startswith("Name")
    assert "worker-0" in result.output
    assert "4 CPU, 32 GiB, 1 GPU" in result.output
    assert "Job Instances" not in result.output
    assert "Total:" not in result.output
    assert "job-abc-worker-deadbeef" not in result.output
    assert "node-a" in result.output
    assert "backend" not in result.output


def test_job_instances_json_uses_rank_when_platform_only_returns_handle(
    monkeypatch,
) -> None:  # noqa: ANN001
    monkeypatch.setattr(job_commands.Config, "from_files_and_env", lambda **kwargs: (object(), []))
    monkeypatch.setattr(job_commands, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(job_commands, "_resolve_web_job_id", lambda **kwargs: "job-abc")
    monkeypatch.setattr(
        job_commands.browser_api_module,
        "list_job_instances",
        lambda *args, **kwargs: (
            [
                {
                    "instance_id": "internal-instance-handle",
                    "instance_type": "worker",
                    "rank": 3,
                }
            ],
            1,
        ),
    )
    monkeypatch.setattr(job_commands, "_close_web_client", lambda: None)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "instances",
            "train-a",
            "--workspace",
            "Test Workspace",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload == {
        "name": "train-a",
        "items": [{"type": "worker", "rank": 3}],
    }
    assert "internal-instance-handle" not in result.output


def test_job_instances_default_budget_keeps_resolution_window_and_notifies(
    monkeypatch,
) -> None:  # noqa: ANN001
    captured = {}

    monkeypatch.setattr(job_commands.Config, "from_files_and_env", lambda **kwargs: (object(), []))
    monkeypatch.setattr(job_commands, "get_web_session", lambda: _FakeSession())

    def fake_resolve(**kwargs):  # noqa: ANN001
        captured["resolve"] = kwargs
        return "job-abc"

    monkeypatch.setattr(
        job_commands,
        "_resolve_web_job_id",
        fake_resolve,
    )
    monkeypatch.setattr(
        job_commands.browser_api_module,
        "list_job_instances",
        lambda job_id, *, limit, session: (
            captured.update({"job_id": job_id, "limit": limit, "session": session})
            or (
                [
                    {
                        "name": f"worker-{index}",
                        "instance_status": "instance_running",
                        "instance_type": "worker",
                    }
                    for index in range(20)
                ],
                25,
            )
        ),
    )
    monkeypatch.setattr(job_commands, "_close_web_client", lambda: None)

    result = CliRunner().invoke(
        cli_main,
        ["job", "instances", "train-a", "--workspace", "Test Workspace"],
    )

    assert result.exit_code == 0, result.output
    assert captured["resolve"]["scan_limit"] == 500
    assert captured["limit"] == 20
    assert "Showing 20 of 25. Use --all for the full list." in result.output

    json_result = CliRunner().invoke(
        cli_main,
        ["--json", "job", "instances", "train-a", "--workspace", "Test Workspace"],
    )
    assert json_result.exit_code == 0, json_result.output
    metadata = json.loads(json_result.output)["data"]
    assert metadata["name"] == "train-a"
    assert len(metadata["items"]) == 20
    assert metadata["shown"] == 20
    assert metadata["total"] == 25
    assert metadata["truncated"] is True
    assert "limit" not in metadata


def test_job_instances_all_expands_once_and_conflict_is_rejected(
    monkeypatch,
) -> None:  # noqa: ANN001
    calls: list[int] = []

    monkeypatch.setattr(job_commands.Config, "from_files_and_env", lambda **kwargs: (object(), []))
    monkeypatch.setattr(job_commands, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(job_commands, "_resolve_web_job_id", lambda **kwargs: "job-abc")
    monkeypatch.setattr(
        job_commands.browser_api_module,
        "list_job_instances",
        lambda job_id, *, limit, session: (
            calls.append(limit)
            or (
                [
                    {
                        "instance_id": f"job-abc-worker-{index}",
                        "name": f"worker-{index}",
                        "instance_status": "instance_running",
                    }
                    for index in range(25 if limit == 25 else 20)
                ],
                25,
            )
        ),
    )
    monkeypatch.setattr(job_commands, "_close_web_client", lambda: None)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "instances",
            "train-a",
            "--workspace",
            "Test Workspace",
            "--all",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert calls == [20, 25]
    assert set(payload) == {"name", "items"}
    assert payload["name"] == "train-a"
    assert len(payload["items"]) == 25
    assert all(set(item) <= {"name", "status", "role", "type", "resource", "rank"} for item in payload["items"])
    assert "job-abc-worker" not in result.output

    monkeypatch.setattr(
        job_commands.Config,
        "from_files_and_env",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("conflicting options must fail before config")
        ),
    )
    conflict = CliRunner().invoke(
        cli_main,
        [
            "job",
            "instances",
            "train-a",
            "--workspace",
            "Test Workspace",
            "--all",
            "--limit",
            "3",
        ],
    )

    assert conflict.exit_code != 0
    assert "Use either --limit or --all, not both." in conflict.output


def test_resolve_web_job_id_pick_selects_matching_job(monkeypatch) -> None:  # noqa: ANN001
    rows = [
        {"name": "train-a", "job_id": "job-1"},
        {"name": "train-a", "job_id": "job-2"},
    ]
    captured = {}

    def fake_list_web_jobs(**kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return rows, []

    monkeypatch.setattr(
        job_commands,
        "get_web_session",
        lambda: pytest.fail(
            "all-workspace live resolution must let _list_web_jobs acquire the session"
        ),
    )
    monkeypatch.setattr(job_commands, "_list_web_jobs", fake_list_web_jobs)

    job_id = job_commands._resolve_web_job_id(
        job="train-a",
        workspace=None,
        all_workspaces=True,
        max_pages=50,
        pick=2,
    )

    assert job_id == "job-2"
    assert captured["limit"] == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("job-smoke-20260507", False),
        ("job-training-v2", False),
        ("job-abc", True),
        ("job-a1b2c3d4", True),
        ("job-12345678-1234-1234-1234-123456789abc", True),
        ("550e8400-e29b-41d4-a716-446655440000", True),
    ],
)
def test_looks_like_job_id_uses_platform_handle_shape(
    value: str,
    expected: bool,
) -> None:
    assert job_commands._looks_like_job_id(value) is expected


def test_resolve_web_job_id_allows_job_prefixed_human_name(monkeypatch) -> None:  # noqa: ANN001
    job_name = "job-smoke-20260507"
    captured = {}

    def fake_list_web_jobs(**kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return [{"name": job_name, "job_id": "job-a1b2c3d4"}], []

    monkeypatch.setattr(job_commands, "_list_web_jobs", fake_list_web_jobs)

    job_id = job_commands._resolve_web_job_id(
        job=job_name,
        workspace=None,
        all_workspaces=True,
        max_pages=50,
    )

    assert job_id == "job-a1b2c3d4"
    assert captured["name"] == job_name


def test_resolve_web_job_id_clear_during_live_lookup_does_not_repopulate_cache(
    monkeypatch,
    tmp_path,
) -> None:  # noqa: ANN001
    workspace_id = "ws-one"
    session = SimpleNamespace(
        base_url="https://inspire.example",
        user_detail={"id": "user-one"},
        all_workspace_names={workspace_id: "CPU"},
    )
    index = job_commands.ResourceIndex(tmp_path / "resource-index.sqlite3")
    scope = job_commands.ResourceScope(
        base_url="https://inspire.example",
        subject_id="user-one",
        resource_type="job",
        workspace_id=workspace_id,
        owner_scope="self",
    )

    monkeypatch.setattr(job_commands, "get_web_session", lambda: session)
    monkeypatch.setattr(
        job_commands,
        "_list_workspace_ids",
        lambda *_args, **_kwargs: [workspace_id],
    )
    monkeypatch.setattr(
        job_commands.ResourceIndex,
        "for_account",
        classmethod(lambda cls, account=None: index),
    )

    def _live_jobs(**_kwargs):
        index.clear()
        return (
            [
                {
                    "job_id": "job-live",
                    "name": "train-a",
                    "workspace_id": workspace_id,
                    "workspace_name": "CPU",
                }
            ],
            1,
        )

    monkeypatch.setattr(job_commands, "_list_web_jobs", _live_jobs)

    job_id = job_commands._resolve_web_job_id(
        job="train-a",
        workspace="CPU",
        all_workspaces=False,
        max_pages=50,
        require_live=True,
    )

    assert job_id == "job-live"
    assert index.list_identities(scope, fresh_only=False) == []


def test_resolve_web_job_id_snapshot_failure_skips_live_cache_write(
    monkeypatch,
    tmp_path,
) -> None:  # noqa: ANN001
    workspace_id = "ws-one"
    session = SimpleNamespace(
        base_url="https://inspire.example",
        user_detail={"id": "user-one"},
        all_workspace_names={workspace_id: "CPU"},
    )
    index = job_commands.ResourceIndex(tmp_path / "resource-index.sqlite3")
    scope = job_commands.ResourceScope(
        base_url="https://inspire.example",
        subject_id="user-one",
        resource_type="job",
        workspace_id=workspace_id,
        owner_scope="self",
    )

    monkeypatch.setattr(job_commands, "get_web_session", lambda: session)
    monkeypatch.setattr(
        job_commands,
        "_list_workspace_ids",
        lambda *_args, **_kwargs: [workspace_id],
    )
    monkeypatch.setattr(
        job_commands.ResourceIndex,
        "for_account",
        classmethod(lambda cls, account=None: index),
    )
    monkeypatch.setattr(
        index,
        "snapshot_token",
        lambda _scope: (_ for _ in ()).throw(OSError("cache unavailable")),
    )
    monkeypatch.setattr(
        job_commands,
        "_list_web_jobs",
        lambda **_kwargs: (
            [
                {
                    "job_id": "job-live",
                    "name": "train-a",
                    "workspace_id": workspace_id,
                    "workspace_name": "CPU",
                }
            ],
            1,
        ),
    )

    job_id = job_commands._resolve_web_job_id(
        job="train-a",
        workspace="CPU",
        all_workspaces=False,
        max_pages=50,
        require_live=True,
    )

    assert job_id == "job-live"
    assert index.list_identities(scope, fresh_only=False) == []


def test_job_shell_command_rejects_job_id_boundary(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(job_commands.Config, "from_files_and_env", lambda **kwargs: (object(), []))
    monkeypatch.setattr(
        job_commands,
        "_list_web_jobs",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("should not resolve platform handles")
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["job", "shell", "job-abc", "--workspace", "Test Workspace"],
    )

    assert result.exit_code != 0
    assert "only accept job names" in result.output


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


def test_remote_shell_url_uses_the_right_instance_key_per_workload(monkeypatch) -> None:  # noqa: ANN001
    """HPC answers only to `instance_id`, and answers nothing at all otherwise.

    Handing `hpc_jobs/instances/exec` an `instance_name` upgrades the socket
    and then returns zero bytes -- no error, no close frame, just a shell that
    never speaks. Measured against a running HPC job: `instance_id` echoed 53
    bytes back, `instance_name` echoed 0. So the key is part of the contract,
    not a spelling preference.
    """
    monkeypatch.setattr(job_shell, "_get_base_url", lambda: "https://qz.sii.edu.cn")

    assert job_shell.build_remote_cmd_ws_url("job-1", "worker-0", workload="job") == (
        "wss://qz.sii.edu.cn/api/v2/train_job/remote_cmd?"
        "job_id=job-1&instance_name=worker-0"
    )
    assert job_shell.build_remote_cmd_ws_url("hpc-1", "proj/pod-0", workload="hpc") == (
        "wss://qz.sii.edu.cn/api/v2/hpc_jobs/instances/exec?"
        "job_id=hpc-1&instance_id=proj%2Fpod-0"
    )
    assert job_shell.build_remote_cmd_ws_url("rj-1", "rj-1-head-x", workload="ray") == (
        "wss://qz.sii.edu.cn/api/v2/ray_job/instances/exec?"
        "job_id=rj-1&instance_id=rj-1-head-x"
    )
    # Serving does not even call its handle `job_id`, and sending that name
    # gets the handshake refused with a bare 200 instead of the 101 upgrade.
    assert job_shell.build_remote_cmd_ws_url("sv-1", "proj/sv-1-0", workload="serving") == (
        "wss://qz.sii.edu.cn/api/v2/inference_servings/instances/exec?"
        "inference_serving_id=sv-1&instance_id=proj%2Fsv-1-0"
    )


def test_remote_shell_refuses_a_workload_it_has_no_measured_route_for(monkeypatch) -> None:  # noqa: ANN001
    """Every route here was measured; an unknown one is refused, not guessed.

    Guessing a key reproduces one of the two failures a wrong key produces --
    a socket that upgrades and then stays silent, or a handshake refused with
    a bare 200 -- both of which read as a hung shell rather than as an
    unsupported command.
    """
    monkeypatch.setattr(job_shell, "_get_base_url", lambda: "https://qz.sii.edu.cn")

    with pytest.raises(job_shell.JobShellError, match="notebook"):
        job_shell.build_remote_cmd_ws_url("x", "y", workload="notebook")


class _FakeShellWebSocket:
    """A websocket whose frames the shell loop can read, plus a real fileno.

    ``run_remote_shell`` selects on this object, so it has to be genuinely
    selectable — a socketpair provides that while the frame queue drives the
    loop.
    """

    def __init__(self, frames: list[tuple[int, bytes]]) -> None:
        import socket as socket_module

        self._frames = list(frames)
        self._reader, self._writer = socket_module.socketpair()
        self.sent: list[str] = []
        self.pongs: list[bytes] = []
        self._writer.sendall(b"\0" * len(self._frames))

    def fileno(self) -> int:
        return self._reader.fileno()

    def __enter__(self) -> "_FakeShellWebSocket":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._reader.close()
        self._writer.close()

    def send_text(self, text: str) -> None:
        self.sent.append(text)

    def recv_frame(self) -> tuple[int, bytes]:
        if not self._frames:
            raise EOFError
        self._reader.recv(1)
        return self._frames.pop(0)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        self.pongs.append(payload)


def _run_shell(monkeypatch, frames, keystrokes: bytes = b"") -> tuple[int, _FakeShellWebSocket]:  # noqa: ANN001
    import io
    import os as os_module

    from inspire.cli.utils import interactive_console

    ws = _FakeShellWebSocket(frames)
    monkeypatch.setattr(job_shell, "_get_base_url", lambda: "https://qz.sii.edu.cn")

    read_fd, write_fd = os_module.pipe()
    os_module.write(write_fd, keystrokes)
    os_module.close(write_fd)
    stdin = os_module.fdopen(read_fd, "rb", buffering=0)
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")

    # The loop selects on stdin too; a pipe is selectable and never a tty, so
    # raw mode stays out of the way.
    assert interactive_console is not None
    code = job_shell.run_remote_shell(
        job_id="job-abc",
        instance_name="worker-0",
        session=_FakeSession(),
        stdin=stdin,
        stdout=stdout,
        websocket_cls=lambda *args, **kwargs: ws,
    )
    stdout.flush()
    return code, ws


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="drives the loop through the POSIX select path; the Windows reader thread's "
    "delivery order is covered in test_interactive_console",
)
def test_run_remote_shell_passes_remote_output_through_verbatim(monkeypatch) -> None:  # noqa: ANN001
    payload = "总用量 4\r\n$ ".encode()
    captured: list[bytes] = []
    monkeypatch.setattr(
        job_shell, "write_stream_output", lambda stream, data: captured.append(data)
    )

    code, _ws = _run_shell(monkeypatch, [(0x1, payload), (0x8, b"")])

    assert code == 0
    assert b"".join(captured) == payload


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="drives the loop through the POSIX select path; the Windows reader thread's "
    "delivery order is covered in test_interactive_console",
)
def test_run_remote_shell_forwards_keystrokes_and_bootstraps_the_size(monkeypatch) -> None:  # noqa: ANN001
    # A close frame ends the loop before stdin is looked at, so the remote has
    # to say something first for the keystrokes to get their turn.
    code, ws = _run_shell(monkeypatch, [(0x1, b"prompt$ "), (0x8, b"")], keystrokes=b"ls -l\r")

    assert code == 0
    assert ws.sent[0] == job_shell.SHELL_BOOTSTRAP
    assert ws.sent[1].startswith("stty columns ")
    assert "ls -l\r" in ws.sent


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="drives the loop through the POSIX select path; the Windows reader thread's "
    "delivery order is covered in test_interactive_console",
)
def test_run_remote_shell_answers_pings_without_printing_them(monkeypatch) -> None:  # noqa: ANN001
    captured: list[bytes] = []
    monkeypatch.setattr(
        job_shell, "write_stream_output", lambda stream, data: captured.append(data)
    )

    code, ws = _run_shell(monkeypatch, [(0x9, b"ping-payload"), (0x8, b"")])

    assert code == 0
    assert ws.pongs == [b"ping-payload"]
    assert captured == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="drives the loop through the POSIX select path; the Windows reader thread's "
    "delivery order is covered in test_interactive_console",
)
def test_run_remote_shell_stops_on_the_shell_exit_marker(monkeypatch) -> None:  # noqa: ANN001
    captured: list[bytes] = []
    monkeypatch.setattr(
        job_shell, "write_stream_output", lambda stream, data: captured.append(data)
    )
    marker_frame = ("bye\r\n" + job_shell.SHELL_EXIT_MARKER).encode()

    code, _ws = _run_shell(monkeypatch, [(0x1, marker_frame), (0x1, b"never read")])

    assert code == 0
    # The marker is the shell announcing its own exit; it never reaches the user.
    assert b"".join(captured) == b"bye\r\n"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="drives the loop through the POSIX select path; the Windows reader thread's "
    "delivery order is covered in test_interactive_console",
)
def test_run_remote_shell_exits_on_the_ctrl_bracket_escape(monkeypatch) -> None:  # noqa: ANN001
    code, ws = _run_shell(
        monkeypatch,
        [(0x1, b"prompt$ ")],
        keystrokes=b"partial" + job_shell.CTRL_RIGHT_BRACKET,
    )

    assert code == 0
    # The escape is local: the chunk carrying it is not forwarded to the remote.
    assert not any("partial" in text for text in ws.sent)
