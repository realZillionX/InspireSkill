"""Remote execution contracts; sockets, SSH and platform requests are all fakes."""

from __future__ import annotations

import ast
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from test_sdk import client as client
from inspire import ExecResult
from inspire.sdk import (
    JobRef,
    NotebookRef,
    HPCJobRef,
    RayJobRef,
    ServingRef,
    ValidationError,
    AuthenticationError,
)
from inspire.services.execution import remote_exec as core
from inspire.platform.web import pty_socket
from inspire.platform.web.browser_api import jupyter_terminal as jt


# Thread watchdog also stops deadlocked event loops and cancellation cleanup.
pytestmark = pytest.mark.timeout(30, method="thread")


class FakeSocket:
    def __init__(self, url, headers, *, timeout):
        self.url, self.headers, self.timeout = url, headers, timeout
        self.frames = [(1, b"pod$ ")]
        self.sent = []
        self.pongs = []
        self.closed = False
        self.fail_auth = False
        self.finish = True

    def connect(self):
        if self.fail_auth:
            raise pty_socket.JobShellAuthError("401")

    def close(self):
        self.closed = True

    def fileno(self):
        return 91

    def has_pending_data(self):
        return bool(self.frames)

    def set_read_timeout(self, timeout):
        assert timeout > 0

    def send_pong(self, data):
        self.pongs.append(data)

    def send_text(self, data):
        self.sent.append(data)
        self.frames += [
            (1, data.encode() + b"\n"),
            (9, b"ping"),
            (1, b"hello "),
            (1, "世".encode()[:1]),
            (1, "世".encode()[1:] + b"\r\n"),
        ]
        if self.finish:
            self.frames += [(1, b"test-marker:ex"), (1, b"it:3"), (1, b"\r\n"), (1, b"unread")]

    def recv_frame(self):
        return self.frames.pop(0)


@pytest.fixture
def sockets(client, monkeypatch):
    client._transport._session.storage_state["cookies"] = [
        {"name": "inspire-session", "value": "fake-session"}
    ]
    created = []
    clock = [0.0]

    def make(*a, **kw):
        socket = FakeSocket(*a, **kw)
        created.append(socket)
        return socket

    def select(read, _write, _error, delay):
        clock[0] += 0.01
        socket = created[-1]
        return (read if socket.frames else [], [], [])

    monkeypatch.setattr(core, "WebSocketClient", make)
    monkeypatch.setattr(core, "new_completion_marker", lambda: "test-marker")
    monkeypatch.setattr(core.select, "select", select)
    monkeypatch.setattr(core.time, "monotonic", lambda: clock[0])
    return created


def job_ref(client, cls=JobRef):
    return cls("example", client.account, client.base_url, "workload-key", "workspace-key")


def job_rows():
    return [
        {"name": "worker-0", "instance_status": "instance_running", "rank": 0, "role": "worker"}
    ]


def test_pty_capture_protocol_and_callback(client, sockets):
    chunks = []
    result = core.exec_over_pty_websocket(
        session=client._transport.session,
        url="wss://example.invalid/exec",
        command="exit 3",
        timeout=2,
        on_output=chunks.append,
    )
    assert result == ExecResult(3, "hello 世\r\n", "hello 世\r\n", "", True, "pty")
    socket = sockets[0]
    assert socket.sent == [jt.build_jupyter_exec_command("exit 3", marker="test-marker")]
    assert socket.headers["Cookie"] == "inspire-session=fake-session"
    assert socket.headers["Origin"] == client.base_url
    assert "Sec-WebSocket-Protocol" not in socket.headers
    assert socket.pongs == [b"ping"]
    assert socket.closed
    assert chunks[0] == "pod$ "
    assert "".join(chunks).endswith("test-marker:exit:3\r\n")
    assert "hello 世\r\n" in "".join(chunks)
    with pytest.raises(FrozenInstanceError):
        result.returncode = 0


def test_pty_timeout_and_bounded_prompt_wait(client, sockets, monkeypatch):
    original = core.WebSocketClient

    def make(*a, **kw):
        socket = original(*a, **kw)
        socket.finish = False
        socket.frames = []
        return socket

    monkeypatch.setattr(core, "WebSocketClient", make)
    result = core.exec_over_pty_websocket(
        session=client._transport.session,
        url="wss://example.invalid",
        command="sleep 30",
        timeout=0.5,
    )
    assert result.returncode == 124 and not result.completed
    assert sockets[0].sent and sockets[0].closed
    assert "hello 世" in result.output


def test_pty_callback_error_closes_without_retry(client, sockets):
    def fail(chunk):
        raise RuntimeError("consumer failed")

    with pytest.raises(RuntimeError, match="consumer failed"):
        core.exec_over_pty_websocket(
            session=client._transport.session,
            url="wss://example.invalid",
            command="echo hi",
            timeout=1,
            on_output=fail,
        )
    assert len(sockets) == 1 and sockets[0].closed


@pytest.mark.parametrize("always_fail", [False, True])
def test_sdk_renews_once_before_sending(client, sockets, monkeypatch, always_fail):
    monkeypatch.setattr(
        "inspire.services.job.job_events.list_all_job_instances", lambda *a, **kw: job_rows()
    )
    original = core.WebSocketClient

    def make(*a, **kw):
        socket = original(*a, **kw)
        socket.fail_auth = always_fail or len(sockets) == 1
        return socket

    monkeypatch.setattr(core, "WebSocketClient", make)
    refreshes = []

    def refresh():
        refreshes.append(1)
        client._transport._session.storage_state["cookies"][0]["value"] = "renewed"

    monkeypatch.setattr(client._transport, "_refresh", refresh)
    monkeypatch.setattr(
        client._transport, "request", lambda *a, **kw: pytest.fail("exec dispatched HTTP")
    )
    if always_fail:
        with pytest.raises(AuthenticationError):
            client.jobs.exec(job_ref(client), command="echo hi")
    else:
        result = client.jobs.exec(job_ref(client), command="echo hi")
        assert result.returncode == 3 and result.instance == "worker-0"
        assert sockets[1].headers["Cookie"] == "inspire-session=renewed"
    assert refreshes == [1]
    assert len(sockets) == 2 and all(s.closed for s in sockets)
    assert sockets[0].sent == []


WORKLOADS = [
    (
        "jobs",
        "job",
        JobRef,
        "inspire.services.job.job_events.list_all_job_instances",
        job_rows(),
        "worker-0",
        "/api/v2/train_job/remote_cmd",
        "job_id",
        "instance_name",
    ),
    (
        "hpc",
        "hpc",
        HPCJobRef,
        "inspire.sdk.hpc.fetch_hpc_instances",
        [
            {"name": "project/worker", "role": "worker", "status": "RUNNING"},
            {"name": "project/launcher", "role": "launcher", "status": "RUNNING"},
        ],
        "project/launcher",
        "/api/v2/hpc_jobs/instances/exec",
        "job_id",
        "instance_id",
    ),
    (
        "ray",
        "ray",
        RayJobRef,
        "inspire.sdk.ray.fetch_ray_instances",
        [{"name": "ray-head-0", "role": "head", "type": "head", "status": "RUNNING"}],
        "ray-head-0",
        "/api/v2/ray_job/instances/exec",
        "job_id",
        "instance_id",
    ),
    (
        "servings",
        "serving",
        ServingRef,
        "inspire.sdk.servings.fetch_serving_instances",
        [
            {"name": "project/sv-0", "status": "STOPPED"},
            {"name": "project/sv-1", "status": "RUNNING"},
            {"name": "project/sv-2", "status": "RUNNING"},
        ],
        "project/sv-1",
        "/api/v2/inference_servings/instances/exec",
        "inference_serving_id",
        "instance_id",
    ),
]


@pytest.mark.parametrize(
    "facade,kind,ref_cls,patch,rows,expected,path,handle_key,instance_key", WORKLOADS
)
def test_sdk_routes_and_defaults(
    client,
    sockets,
    monkeypatch,
    facade,
    kind,
    ref_cls,
    patch,
    rows,
    expected,
    path,
    handle_key,
    instance_key,
):
    monkeypatch.setattr(patch, lambda *a, **kw: rows if kind == "job" else (rows, len(rows)))
    result = getattr(client, facade).exec(job_ref(client, ref_cls), command="exit 3")
    assert result.completed and result.returncode == 3 and result.instance == expected
    url = urlsplit(sockets[0].url)
    assert url.path == path
    assert parse_qs(url.query) == {handle_key: ["workload-key"], instance_key: [expected]}


@pytest.mark.parametrize(
    "selector,expected",
    [("rank=1", "worker-1"), ("1", "worker-1"), ("leader", "worker-0"), ("worker-1", "worker-1")],
)
def test_job_selectors(selector, expected):
    rows = [dict(job_rows()[0], role="leader"), dict(job_rows()[0], name="worker-1", rank=1)]
    assert core.select_exec_instance("job", rows, selector) == expected
    with pytest.raises(pty_socket.JobShellError, match="worker-0.*|Multiple running"):
        core.select_exec_instance("job", rows)


@pytest.mark.parametrize(
    "kind,rows,selector",
    [
        ("job", job_rows() + [dict(job_rows()[0], name="worker-1", rank=1)], "worker"),
        (
            "hpc",
            [
                {"name": "a", "role": "launcher", "status": "RUNNING"},
                {"name": "b", "role": "launcher", "status": "RUNNING"},
            ],
            None,
        ),
        (
            "ray",
            [
                {"name": "a-head-0", "type": "head", "status": "RUNNING"},
                {"name": "b-head-0", "type": "head", "status": "RUNNING"},
            ],
            None,
        ),
        (
            "serving",
            [
                {"name": "a", "role": "leader", "status": "RUNNING"},
                {"name": "b", "role": "leader", "status": "RUNNING"},
            ],
            "leader",
        ),
    ],
)
def test_ambiguous_instance_lists_candidates(kind, rows, selector):
    with pytest.raises(ValueError, match="Multiple.*Candidates") as error:
        core.select_exec_instance(kind, rows, selector)
    for row in rows:
        assert row["name"] in str(error.value)


@pytest.mark.parametrize("kind", ["job", "hpc", "ray", "serving"])
def test_no_running_instance(kind):
    with pytest.raises((ValueError, pty_socket.JobShellError), match="No running"):
        core.select_exec_instance(kind, [{"name": "stopped", "status": "STOPPED"}])


@pytest.mark.parametrize(
    "transport,bridge,expected",
    [
        ("auto", "cached", "ssh"),
        ("auto", None, "jupyter"),
        ("jupyter", "cached", "jupyter"),
        ("ssh", "cached", "ssh"),
    ],
)
def test_notebook_transport_and_command(client, monkeypatch, transport, bridge, expected):
    calls = []
    monkeypatch.setattr(core, "cached_notebook_bridge", lambda **kw: bridge)
    client._config.remote_env = {"KEY": "config", "FIRST": "a b"}

    def fake(kind, **kw):
        calls.append((kind, kw))
        return ExecResult(0, "ok", "ok", "", True, kind)

    monkeypatch.setattr(core, "exec_in_notebook_ssh", lambda **kw: fake("ssh", **kw))
    monkeypatch.setattr(core, "exec_in_notebook_jupyter", lambda **kw: fake("jupyter", **kw))
    result = client.notebooks.exec(
        job_ref(client, NotebookRef),
        command="pwd",
        transport=transport,
        cwd="/a b",
        env={"KEY": "caller", "EMPTY": ""},
    )
    assert result.transport == expected
    assert calls[0][1]["command"] == (
        "export KEY=config && export FIRST='a b' && export KEY=caller && export EMPTY='' && cd \"/a b\" && pwd"
    )
    if expected == "ssh":
        assert calls[0][1]["account"] == client.account
    else:
        assert calls[0][1]["session"] is client._transport.session


def test_notebook_missing_ssh_hint(client, monkeypatch):
    monkeypatch.setattr(core, "cached_notebook_bridge", lambda **kw: None)
    with pytest.raises(ValidationError, match="inspire notebook connection refresh example"):
        client.notebooks.exec(job_ref(client, NotebookRef), command="pwd", transport="ssh")


@pytest.mark.parametrize("streaming", [False, True])
def test_ssh_preserves_streams_and_timeout(client, monkeypatch, streaming):
    config = object()
    monkeypatch.setattr(core, "load_tunnel_config", lambda **kw: config)
    seen = []

    def run(command, **kw):
        assert kw["config"] is config and kw["bridge_name"] == "cached"
        assert command == "echo hi" and kw["timeout"] == 5
        kw["output_callback"]("out")
        kw["stderr_callback"]("err")
        return 7

    monkeypatch.setattr(core, "run_ssh_command_streaming", run)
    result = core.exec_in_notebook_ssh(
        bridge_name="cached",
        account=client.account,
        command="echo hi",
        timeout=5,
        on_output=seen.append if streaming else None,
    )
    assert result == ExecResult(7, "outerr", "out", "err", True, "ssh")
    assert seen == (["out", "err"] if streaming else [])

    def timed_out(*a, **kw):
        kw["output_callback"]("partial")
        raise subprocess.TimeoutExpired([], 5, output=b"partial", stderr=b"err")

    monkeypatch.setattr(core, "run_ssh_command_streaming", timed_out)
    result = core.exec_in_notebook_ssh(
        bridge_name="cached",
        account=client.account,
        command="echo hi",
        timeout=5,
        on_output=seen.append if streaming else None,
    )
    assert result.returncode == 124 and not result.completed and result.stdout == "partial"


def test_cached_bridge_identity_and_availability(client, monkeypatch):
    from inspire.bridge.tunnel import BridgeProfile, TunnelConfig

    config = TunnelConfig()
    wrong = BridgeProfile(
        name="wrong",
        proxy_url="https://example.invalid",
        notebook_id="other",
        workspace_id="workspace-key",
    )
    bridge = BridgeProfile(
        name="right",
        proxy_url="https://example.invalid",
        notebook_id="workload-key",
        workspace_id="workspace-key",
    )
    config.add_bridge(wrong)
    config.add_bridge(bridge)
    calls = []
    monkeypatch.setattr(core, "load_tunnel_config", lambda **kw: config)
    monkeypatch.setattr(
        core,
        "read_target_cache",
        lambda: {
            "targets": {
                "stale": {
                    "account": client.account,
                    "notebook_id": "workload-key",
                    "workspace_id": "workspace-key",
                    "bridge_name": "wrong",
                }
            }
        },
    )
    monkeypatch.setattr(core.tunnel, "is_tunnel_available", lambda **kw: calls.append(kw) or True)
    assert (
        core.cached_notebook_bridge(
            notebook_id="workload-key", workspace_id="workspace-key", account=client.account
        )
        == "right"
    )
    assert [c["bridge_name"] for c in calls] == ["right"]
    assert (
        core.cached_notebook_bridge(
            notebook_id="workload-key", workspace_id="wrong", account=client.account
        )
        is None
    )
    monkeypatch.setattr(core.tunnel, "is_tunnel_available", lambda **kw: False)
    assert (
        core.cached_notebook_bridge(
            notebook_id="workload-key", workspace_id="workspace-key", account=client.account
        )
        is None
    )


def test_jupyter_capture_callback_and_auth(client, monkeypatch):
    chunks = []
    calls = []

    def capture(**kw):
        calls.append(kw)
        kw["on_output"]("output")
        return jt.JupyterCommandResult(3, "output", True, "marker")

    monkeypatch.setattr(core, "run_command_capture_in_notebook", capture)
    result = core.exec_in_notebook_jupyter(
        session=client._transport.session,
        notebook_id="nb",
        command="false",
        timeout=2,
        on_output=chunks.append,
    )
    assert result == ExecResult(3, "output", "output", "", True, "jupyter")
    assert chunks == ["output"] and calls[0]["notebook_id"] == "nb"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 0},
        {"timeout": float("nan")},
        {"env": {"BAD;KEY": "x"}},
        {"env": {"X": 3}},
        {"on_output": 7},
        {"command": ""},
    ],
)
def test_invalid_exec_inputs_do_not_lookup(client, monkeypatch, kwargs):
    monkeypatch.setattr(client.jobs, "_resolve", lambda *a: pytest.fail("lookup before validation"))
    with pytest.raises(ValidationError):
        client.jobs.exec(job_ref(client), **dict({"command": "pwd"}, **kwargs))


def test_new_layers_do_not_import_cli_or_ui():
    paths = [
        Path("inspire/platform/web/pty_socket.py"),
        Path("inspire/platform/web/jupyter_urls.py"),
        Path("inspire/platform/web/browser_api/jupyter_terminal.py"),
    ]
    paths += list(Path("inspire/services").rglob("*.py")) + list(Path("inspire/sdk").glob("*.py"))
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [n.name for n in node.names]
                if isinstance(node, ast.Import)
                else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            )
            assert not any(
                name == prefix or name.startswith(prefix + ".")
                for name in names
                for prefix in ("inspire.cli", "click", "rich", "playwright")
            ), path


def test_command_cwd_is_literal_and_caller_env_is_not_local_expansion():
    assert core.build_remote_command("pwd", cwd='/$HOME/$(whoami)/`id`/"q"') == (
        'cd "/\\$HOME/\\$(whoami)/\\`id\\`/\\"q\\"" && pwd'
    )
    assert core.build_remote_command("env", env={"X": "$HOME", "Y": ""}) == (
        "export X='$HOME' && export Y='' && env"
    )


@pytest.mark.parametrize("expired", [False, True])
def test_notebook_jupyter_end_to_end_fake_socket(client, monkeypatch, expired):
    from contextlib import contextmanager
    from test_notebook_jupyter_terminal import _FakeWebSocket

    sessions = []
    sockets = []
    chunks = []
    refreshes = []

    @contextmanager
    def terminal(session, notebook_id, **kwargs):
        sessions.append(session)
        assert notebook_id == "workload-key"
        yield jt._JupyterTerminal("https://example.invalid/lab", "1", "wss://example.invalid/ws")

    def socket(*a, **kw):
        ws = _FakeWebSocket(
            ["$ ", "echo 'eA==' | base64 -d | bash\r\n", "hi\r\n", "fixed-marker:exit:3\r\n"]
        )
        if expired and not sockets:

            class ExpiredSocket(_FakeWebSocket):
                def __enter__(self):
                    raise pty_socket.JobShellAuthError("401")

            ws = ExpiredSocket([])
        sockets.append(ws)
        return ws

    monkeypatch.setattr(jt, "_jupyter_terminal", terminal)
    monkeypatch.setattr(jt, "new_completion_marker", lambda: "fixed-marker")
    monkeypatch.setattr(pty_socket, "WebSocketClient", socket)
    monkeypatch.setattr(jt.select, "select", lambda r, w, e, t: (r, [], []))
    monkeypatch.setattr(client._transport, "_refresh", lambda: refreshes.append(1))
    result = client.notebooks.exec(
        job_ref(client, NotebookRef),
        command="exit 3",
        transport="jupyter",
        timeout=1,
        on_output=chunks.append,
    )
    assert result == ExecResult(3, "hi\r\n", "hi\r\n", "", True, "jupyter")
    assert chunks == [
        "$ ",
        "echo 'eA==' | base64 -d | bash\r\n",
        "hi\r\n",
        "fixed-marker:exit:3\r\n",
    ]
    assert len(sessions) == (2 if expired else 1)
    assert refreshes == ([1] if expired else [])


def test_pty_handshake_failure_closes_raw_socket(monkeypatch):
    class RawSocket:
        closed = False

        def settimeout(self, timeout):
            pass

        def sendall(self, data):
            pass

        def recv(self, count):
            raise TimeoutError("partial handshake")

        def close(self):
            self.closed = True

    raw = RawSocket()
    socket = pty_socket.WebSocketClient("ws://example.invalid/exec", {})
    monkeypatch.setattr(socket, "_create_socket", lambda *a: raw)
    with pytest.raises(TimeoutError):
        socket.connect()
    assert raw.closed and socket.sock is None


def test_ssh_streaming_login_shell_with_fake_process(client, monkeypatch):
    import io
    from types import SimpleNamespace
    from inspire.bridge.tunnel import ssh_exec

    calls = []
    process = SimpleNamespace(
        stdin=io.StringIO(),
        stdout=io.TextIOWrapper(io.BytesIO(b"out")),
        stderr=io.TextIOWrapper(io.BytesIO(b"err")),
        poll=lambda: 7,
        wait=lambda **kw: 7,
    )
    monkeypatch.setattr(ssh_exec, "_resolve_bridge_and_proxy", lambda *a, **kw: (None, None, None))
    monkeypatch.setattr(ssh_exec, "_build_ssh_base_args", lambda **kw: ["ssh", "fake-host"])
    # Logging needs the bridge name even when no real bridge is used.
    monkeypatch.setattr(
        ssh_exec,
        "_resolve_bridge_and_proxy",
        lambda *a, **kw: (None, SimpleNamespace(name="cached"), None),
    )
    monkeypatch.setattr(ssh_exec, "build_ssh_process_env", lambda: {})
    monkeypatch.setattr(
        ssh_exec.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)) or process
    )
    out, err = [], []
    assert (
        ssh_exec.run_ssh_command_streaming(
            "echo hi", output_callback=out.append, stderr_callback=err.append, timeout=1
        )
        == 7
    )
    assert out == ["out"] and err == ["err"]
    assert calls[0][0][0][-1] == "bash -l"
    assert calls[0][1]["stderr"] == subprocess.PIPE
    assert process.stdout.closed and process.stderr.closed


def test_sdk_does_not_retry_auth_error_after_output(client, sockets, monkeypatch):
    from inspire.platform.web.session.models import SessionExpiredError

    monkeypatch.setattr(
        "inspire.services.job.job_events.list_all_job_instances", lambda *a, **kw: job_rows()
    )
    monkeypatch.setattr(client._transport, "_refresh", lambda: pytest.fail("replayed after output"))

    def fail(chunk):
        raise SessionExpiredError("callback failed")

    with pytest.raises(AuthenticationError):
        client.jobs.exec(job_ref(client), command="echo hi", on_output=fail)
    assert len(sockets) == 1 and sockets[0].closed


@pytest.mark.parametrize("transport", ["pty", "jupyter", "ssh"])
@pytest.mark.parametrize("capture", [True, False])
def test_large_stream_capture_and_file(client, sockets, monkeypatch, tmp_path, transport, capture):
    import io
    from contextlib import contextmanager
    from test_notebook_jupyter_terminal import _FakeWebSocket
    from inspire.sdk import iter_output_file
    from inspire.exec_output import TerminalScanner

    body = ["HEAD\n"] + ["世" * 1024 + "\n"] * 256 + ["TAIL\n"]
    terminal = ["$ ", "echo 'eA==' | base64 -d | bash\r\n"] + body + ["test-marker:ex", "it:9\r\n"]
    chunks = body if transport == "ssh" else terminal
    seen = []
    scanned = []
    original_scan = TerminalScanner.scan_window

    def scan(self, window):
        scanned.append(len(window))
        assert len(window) <= 2 * self.window_size
        original_scan(self, window)

    monkeypatch.setattr(TerminalScanner, "scan_window", scan)
    if transport == "pty":
        original = core.WebSocketClient

        def make(*a, **kw):
            ws = original(*a, **kw)
            ws.frames = [(1, text.encode()) for text in terminal]
            ws.send_text = lambda text: ws.sent.append(text)
            return ws

        monkeypatch.setattr(core, "WebSocketClient", make)

        def run(**kw):
            return core.exec_over_pty_websocket(
                session=client._transport.session, url="wss://example.invalid", **kw
            )
    elif transport == "jupyter":

        @contextmanager
        def context(*a, **kw):
            yield jt._JupyterTerminal("https://example.invalid", "1", "wss://example.invalid")

        monkeypatch.setattr(jt, "_jupyter_terminal", context)
        monkeypatch.setattr(jt, "new_completion_marker", lambda: "test-marker")
        monkeypatch.setattr(
            pty_socket, "WebSocketClient", lambda *a, **kw: _FakeWebSocket(terminal)
        )
        monkeypatch.setattr(jt.select, "select", lambda r, w, e, t: (r, [], []))

        def run(**kw):
            return core.exec_in_notebook_jupyter(
                session=client._transport.session, notebook_id="fake", **kw
            )
    else:
        monkeypatch.setattr(core, "load_tunnel_config", lambda **kw: object())

        def ssh(*a, **kw):
            for chunk in body:
                kw["output_callback"](chunk)
            return 9

        monkeypatch.setattr(core, "run_ssh_command_streaming", ssh)

        def run(**kw):
            return core.exec_in_notebook_ssh(bridge_name="fake", account="fake", **kw)

    path = tmp_path / "output.txt"
    result = run(
        command="fake",
        timeout=60,
        max_output_bytes=1024,
        capture=capture,
        output_to=path,
        on_output=seen.append,
    )
    assert result.completed and result.returncode == 9
    assert result.total_output_bytes == len("".join(chunks).encode())
    assert result.truncated is capture
    assert len(result.output.encode()) <= 1024
    if capture:
        assert "HEAD" in result.output and "TAIL" in result.output
        assert "output truncated" in result.output
        assert "base64" not in result.output
    else:
        assert result.output == result.stdout == result.stderr == ""
    assert seen == chunks
    pages = list(iter_output_file(path, chunk_size=317))
    assert max(map(len, pages)) <= 317
    assert "".join(pages) == "".join(chunks)
    assert len(scanned) <= sum(len(c) // 75 + 1 for c in chunks)
    # Caller-owned handles stay open.
    handle = io.StringIO()
    run(command="fake", timeout=60, capture=False, output_to=handle)
    assert not handle.closed and handle.getvalue() == "".join(chunks)


def test_scanner_work_is_linear_and_marker_inside_large_frame():
    from inspire.exec_output import TerminalScanner

    for frames in (100, 200):
        scanner = TerminalScanner("marker")
        scanned = []
        original = scanner.scan_window

        def scan(window):
            scanned.append(len(window))
            original(window)

        scanner.scan_window = scan
        for _ in range(frames):
            scanner.feed("x" * 64)
        assert len(scanned) == frames
        assert sum(scanned) <= frames * 2 * scanner.window_size
        scanner.feed("marker:exit:7\n" + "z" * 10000)
        assert scanner.returncode == 7


@pytest.mark.parametrize("kwargs", [
    {"max_output_bytes": -1}, {"max_output_bytes": True},
    {"max_output_bytes": 1.5}, {"capture": "no"}, {"output_to": 7},
])
def test_capture_validation_before_resolution(client, monkeypatch, kwargs):
    monkeypatch.setattr(client.jobs, "_resolve", lambda *a: pytest.fail("lookup"))
    with pytest.raises(ValidationError):
        client.jobs.exec(job_ref(client), command="pwd", **kwargs)


def test_unlimited_capture_and_split_unicode():
    from inspire.exec_output import OutputBuffer

    buffer = OutputBuffer(None)
    for _ in range(100):
        buffer.feed("世\r\n")
    assert buffer.text() == "世\r\n" * 100
    assert buffer.total == 500 and not buffer.truncated
    buffer = OutputBuffer(65)
    buffer.feed("世" * 100)
    assert len(buffer.text().encode()) <= 65 and buffer.truncated


@pytest.mark.parametrize("capture", [True, False])
def test_jupyter_sink_failure_is_not_swallowed(client, monkeypatch, capture):
    from contextlib import contextmanager
    from test_notebook_jupyter_terminal import _FakeWebSocket

    @contextmanager
    def terminal(*a, **kw):
        yield jt._JupyterTerminal("https://example.invalid", "1", "wss://example.invalid")

    class BrokenWriter:
        def write(self, text):
            raise OSError("disk full")

    monkeypatch.setattr(jt, "_jupyter_terminal", terminal)
    monkeypatch.setattr(pty_socket, "WebSocketClient", lambda *a, **kw: _FakeWebSocket(["$ "]))
    monkeypatch.setattr(jt.select, "select", lambda r, w, e, t: (r, [], []))
    with pytest.raises(OSError, match="disk full"):
        core.exec_in_notebook_jupyter(
            session=client._transport.session, notebook_id="fake", command="fake",
            timeout=1, capture=capture, output_to=BrokenWriter(),
        )


def test_no_capture_marker_without_final_newline():
    output = jt.TerminalOutput("marker", 1024, False)
    output.feed("text\nmarker:exit:17")
    result = output.result()
    assert result.completed and result.returncode == 17 and result.output == ""


def test_minimum_capture_budget():
    from inspire.exec_output import OutputBuffer, ELISION

    buffer = OutputBuffer(len(ELISION.encode()))
    buffer.feed("x" * 1000)
    assert buffer.text() == ELISION


@pytest.mark.parametrize("through_facade", [False, True], ids=["core", "jobs-exec"])
def test_pty_connect_timeout_is_incomplete(client, sockets, monkeypatch, through_facade):
    def timeout(self):
        raise TimeoutError("handshake timed out")

    monkeypatch.setattr(FakeSocket, "connect", timeout)
    monkeypatch.setattr(
        "inspire.services.job.job_events.list_all_job_instances", lambda *a, **kw: job_rows(),
    )
    if through_facade:
        result = client.jobs.exec(job_ref(client), command="echo hi", timeout=5)
    else:
        result = core.exec_over_pty_websocket(
            session=client._transport.session, url="wss://example.invalid/exec",
            command="echo hi", timeout=5,
        )
    assert result.returncode == 124
    assert result.completed is False
    assert result.output == ""
    assert len(sockets) == 1 and sockets[0].closed
    assert sockets[0].sent == []


@pytest.mark.parametrize("limit", [128, None])
def test_capture_off_retains_no_buffer_bytes(limit):
    from inspire.exec_output import OutputBuffer

    buffer = OutputBuffer(limit, capture=False)
    for _ in range(10):
        buffer.feed("hello 世界" * 100)
        assert buffer.head == bytearray()
        assert list(buffer.tail) == []
        assert buffer.tail_size == 0
    assert buffer.total == len(("hello 世界" * 1000).encode())
    assert buffer.text() == ""
