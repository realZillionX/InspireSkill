from __future__ import annotations

import base64
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from inspire.platform.web.browser_api import jupyter_terminal as jt


def test_build_exec_command_hides_done_marker_from_terminal_echo() -> None:
    command = jt.build_jupyter_exec_command("echo hi", marker="__INSPIRE_DONE_abc__")

    assert command.startswith("echo '")
    assert command.endswith("' | base64 -d | bash\r")
    encoded = command[len("echo '") : -len("' | base64 -d | bash\r")]
    decoded = base64.b64decode(encoded).decode()
    assert "echo hi" in decoded
    assert ") </dev/null" in decoded
    assert "__INSPIRE_DONE_abc__" in decoded
    assert "__INSPIRE_DONE_abc__" not in encoded


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="runs a real bash; on Windows `bash` is the WSL launcher",
)
def test_build_exec_command_reports_exit_even_when_user_command_exits_shell() -> None:
    command = jt.build_jupyter_exec_command("exit 7", marker="__INSPIRE_DONE_exit__")
    encoded = command[len("echo '") : -len("' | base64 -d | bash\r")]
    decoded = base64.b64decode(encoded).decode()

    result = subprocess.run(  # noqa: S603
        ["bash", "-c", decoded],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 7
    assert "__INSPIRE_DONE_exit__:exit:7" in result.stdout


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="runs a real bash; on Windows `bash` is the WSL launcher",
)
def test_build_exec_command_keeps_control_tail_out_of_user_stdin() -> None:
    """An stdin reader must not consume the wrapper's completion marker."""
    command = jt.build_jupyter_exec_command("cat", marker="__INSPIRE_DONE_stdin__")
    encoded = command[len("echo '") : -len("' | base64 -d | bash\r")]
    decoded = base64.b64decode(encoded).decode()

    # Feeding the script on bash's stdin mirrors `base64 -d | bash`. Without
    # the /dev/null boundary, `cat` prints the remaining control script and the
    # parent never executes the marker line.
    result = subprocess.run(  # noqa: S603
        ["bash"],
        input=decoded,
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert result.returncode == 0
    assert result.stdout == "\n__INSPIRE_DONE_stdin__:exit:0\n"


def test_parse_command_output_extracts_returncode_and_removes_marker() -> None:
    raw = "hello\n__INSPIRE_DONE_abc__:exit:7\n"
    result = jt.parse_jupyter_exec_output(raw, marker="__INSPIRE_DONE_abc__")

    assert result.returncode == 7
    assert result.output == "hello\n"
    assert result.completed is True


def test_parse_command_output_strips_terminal_banner_and_echo() -> None:
    raw = (
        "\x1b[32m══════════════════════════ 欢迎使用 Inspire Studio ══════════════════════════\x1b[0m\r\n"
        "Tips:\r\n"
        "\x1b[?2004h[root:user]$ echo 'YWJj' | base64 -d | bash\r\n"
        "\x1b[?2004l\r"
        "exec-ok\r\n"
        "host=remote-pod\r\n"
        "\r\n__INSPIRE_DONE_abc__:exit:0\r\n"
    )

    result = jt.parse_jupyter_exec_output(raw, marker="__INSPIRE_DONE_abc__")

    assert result.returncode == 0
    assert result.output == "exec-ok\r\nhost=remote-pod\r\n\r\n"
    assert "欢迎使用 Inspire Studio" not in result.output
    assert "base64 -d" not in result.output


def test_parse_command_output_marks_missing_marker_unknown() -> None:
    result = jt.parse_jupyter_exec_output("partial output", marker="__INSPIRE_DONE_abc__")

    assert result.returncode == 124
    assert result.output == "partial output"
    assert result.completed is False


class _FakeHttp:
    """Stands in for a `requests.Session` primed with the notebook cookies."""

    def __init__(self, *, post_status: int = 200, term_name: str = "1") -> None:
        self.cookies = {"_xsrf": "xsrf-token"}
        self._post_status = post_status
        self._term_name = term_name
        self.calls: list[tuple[str, str, dict]] = []
        self.closed = False

    def _record(self, verb, url, headers):  # noqa: ANN001, ANN202
        self.calls.append((verb, url, dict(headers or {})))

    def get(self, url, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self._record("GET", url, kwargs.get("headers"))
        return SimpleNamespace(status_code=200)

    def post(self, url, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self._record("POST", url, kwargs.get("headers"))
        return SimpleNamespace(
            status_code=self._post_status,
            json=lambda: {"name": self._term_name},
        )

    def delete(self, url, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self._record("DELETE", url, kwargs.get("headers"))
        return SimpleNamespace(status_code=204)

    def close(self) -> None:
        self.closed = True


def _patch_terminal_http(monkeypatch, http, *, jupyter_url):  # noqa: ANN001, ANN202
    monkeypatch.setattr(jt, "build_requests_session", lambda *_a, **_k: http)
    monkeypatch.setattr(jt, "_notebook_jupyter_url", lambda *_a, **_k: jupyter_url)
    monkeypatch.setattr(
        jt.rtunnel_module,
        "_build_terminal_websocket_url",
        lambda lab_url, term_name: f"wss://nb.example.com/terminals/websocket/{term_name}",
    )


def test_jupyter_terminal_creates_and_deletes_over_plain_http(monkeypatch) -> None:  # noqa: ANN001
    """No browser anywhere: `_xsrf` is a cookie, and the rest is REST.

    Chromium used to be started solely to obtain that cookie and to issue two
    API calls the platform is happy to take over ordinary HTTP.
    """
    http = _FakeHttp()
    _patch_terminal_http(monkeypatch, http, jupyter_url="https://nb.example.com/jupyter/nb-1/tok/lab")

    with jt._jupyter_terminal(object(), "nb-1") as term:
        assert term is not None
        assert term.name == "1"
        assert term.ws_url == "wss://nb.example.com/terminals/websocket/1"

    verbs = [verb for verb, _url, _headers in http.calls]
    assert verbs == ["GET", "POST", "DELETE"]
    # The Jupyter server rejects state-changing calls without the echoed token.
    assert http.calls[1][2]["X-XSRFToken"] == "xsrf-token"
    assert http.calls[1][1].endswith("/api/terminals")
    assert http.calls[2][1].endswith("/api/terminals/1")
    assert http.closed is True


def test_jupyter_terminal_deletes_even_when_the_body_raises(monkeypatch) -> None:  # noqa: ANN001
    http = _FakeHttp()
    _patch_terminal_http(monkeypatch, http, jupyter_url="https://nb.example.com/jupyter/nb-1/tok/lab")

    with pytest.raises(RuntimeError):
        with jt._jupyter_terminal(object(), "nb-1"):
            raise RuntimeError("command blew up")

    assert [verb for verb, _u, _h in http.calls][-1] == "DELETE"


def test_jupyter_terminal_yields_none_for_a_stopped_notebook(monkeypatch) -> None:  # noqa: ANN001
    """A STOPPED notebook answers `GetNotebookAccessUrl` with empty strings."""
    monkeypatch.setattr(jt, "_notebook_jupyter_url", lambda *_a, **_k: "")

    with jt._jupyter_terminal(object(), "nb-1") as term:
        assert term is None


def test_jupyter_terminal_yields_none_when_creation_is_refused(monkeypatch) -> None:  # noqa: ANN001
    http = _FakeHttp(post_status=403)
    _patch_terminal_http(monkeypatch, http, jupyter_url="https://nb.example.com/jupyter/nb-1/tok/lab")

    with jt._jupyter_terminal(object(), "nb-1") as term:
        assert term is None
    # Nothing was created, so nothing is deleted.
    assert "DELETE" not in [verb for verb, _u, _h in http.calls]


class _FakeWebSocket:
    """Replays terminal frames, and records what was written back."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.sent: list[str] = []

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        return False

    def fileno(self) -> int:
        return 0

    def send_text(self, text: str) -> None:
        self.sent.append(text)

    def recv_frame(self):  # noqa: ANN201
        if not self._frames:
            raise EOFError
        return 0x1, json.dumps(["stdout", self._frames.pop(0)]).encode()


def _patch_capture_socket(monkeypatch, ws):  # noqa: ANN001, ANN202
    import inspire.cli.utils.job_shell as job_shell

    monkeypatch.setattr(job_shell, "_WebSocketClient", lambda *_a, **_k: ws)
    monkeypatch.setattr(jt.select, "select", lambda r, _w, _x, _t=None: (r, [], []))
    monkeypatch.setattr(jt, "_jupyter_ws_headers", lambda *_a, **_k: {})


def test_capture_sends_on_the_prompt_and_stops_at_the_marker(monkeypatch) -> None:  # noqa: ANN001
    """The Python port keeps the protocol the in-page JavaScript used."""
    marker = "__INSPIRE_DONE_abc__"
    ws = _FakeWebSocket(
        [
            "user@pod:~$ ",
            # The PTY echoes the command back; the prelude stripper cuts to here.
            "echo 'ZWNobyBoaQo=' | base64 -d | bash\r\n",
            "hello\n",
            f"{marker}:exit:0 \n",
        ]
    )
    _patch_capture_socket(monkeypatch, ws)

    result = jt._capture_terminal_output(
        ws_url="wss://nb.example.com/terminals/websocket/1",
        session=object(),
        stdin_data="echo hi\r",
        timeout_ms=5000,
        marker=marker,
    )

    assert result is not None
    assert result.completed is True
    assert result.returncode == 0
    assert result.output == "hello\n"
    # Sent once, only after the prompt showed up.
    assert ws.sent == [json.dumps(["stdin", "echo hi\r"])]


def test_capture_chunks_stdin_the_pty_cannot_swallow_whole(monkeypatch) -> None:  # noqa: ANN001
    marker = "__INSPIRE_DONE_abc__"
    payload = "x" * (jt._STDIN_CHUNK * 2 + 5)
    ws = _FakeWebSocket(["$ ", f"{marker}:exit:0 \n"])
    _patch_capture_socket(monkeypatch, ws)
    monkeypatch.setattr(jt.time, "sleep", lambda _s: None)

    jt._capture_terminal_output(
        ws_url="wss://nb.example.com/terminals/websocket/1",
        session=object(),
        stdin_data=payload,
        timeout_ms=5000,
        marker=marker,
    )

    assert len(ws.sent) == 3
    assert "".join(json.loads(frame)[1] for frame in ws.sent) == payload


def test_capture_reports_unfinished_when_the_marker_never_arrives(monkeypatch) -> None:  # noqa: ANN001
    ws = _FakeWebSocket(["$ ", "partial output"])
    _patch_capture_socket(monkeypatch, ws)

    result = jt._capture_terminal_output(
        ws_url="wss://nb.example.com/terminals/websocket/1",
        session=object(),
        stdin_data="echo hi\r",
        timeout_ms=5000,
        marker="__INSPIRE_DONE_abc__",
    )

    assert result is not None
    assert result.completed is False
    assert result.returncode == jt.MISSING_MARKER_RETURN_CODE


def test_capture_logs_websocket_failure(monkeypatch, caplog) -> None:  # noqa: ANN001
    import inspire.cli.utils.job_shell as job_shell

    class _BrokenWebSocket:
        def __enter__(self):
            raise ConnectionRefusedError("proxy refused connection")

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

    monkeypatch.setattr(job_shell, "_WebSocketClient", lambda *_a, **_k: _BrokenWebSocket())

    with caplog.at_level("DEBUG", logger=jt.__name__):
        result = jt._capture_terminal_output(
            ws_url="wss://nb.example.com/terminals/websocket/1",
            session=SimpleNamespace(storage_state={"cookies": []}, cookies={}),
            stdin_data="echo hi\r",
            timeout_ms=5000,
            marker="__INSPIRE_DONE_abc__",
        )

    assert result is None
    assert "JupyterTerminal WebSocket failed" in caplog.text
    assert "ConnectionRefusedError" in caplog.text


def test_command_capture_runs_without_playwright(monkeypatch) -> None:  # noqa: ANN001
    """The whole point: `notebook exec` no longer starts a browser."""
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    http = _FakeHttp()
    _patch_terminal_http(monkeypatch, http, jupyter_url="https://nb.example.com/jupyter/nb-1/tok/lab")
    seen: dict[str, object] = {}

    def _capture(**kwargs):  # noqa: ANN003, ANN202
        seen.update(kwargs)
        return jt.JupyterCommandResult(
            returncode=0, output="ok\n", completed=True, marker=kwargs["marker"]
        )

    monkeypatch.setattr(jt, "_capture_terminal_output", _capture)

    result = jt._run_command_capture_in_notebook_sync(
        notebook_id="nb-1",
        command="echo ok",
        session=SimpleNamespace(storage_state={"cookies": []}, account="secondary"),
        timeout=9,
        marker="done",
    )

    assert result.returncode == 0
    assert seen["ws_url"] == "wss://nb.example.com/terminals/websocket/1"
    assert seen["timeout_ms"] == 9000
    assert http.closed is True


def test_build_jupyter_terminal_ws_url_uses_existing_rtunnel_helper(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        jt.rtunnel_module,
        "_build_terminal_websocket_url",
        lambda lab_url, term_name: f"wss://example.test/{term_name}",
    )

    assert (
        jt.build_jupyter_terminal_ws_url("https://nb.example.com/lab", "3")
        == "wss://example.test/3"
    )
