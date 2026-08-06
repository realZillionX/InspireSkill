from __future__ import annotations

import base64
import subprocess
import sys
from types import ModuleType, SimpleNamespace

from inspire.platform.web.browser_api import jupyter_terminal as jt


def test_build_exec_command_hides_done_marker_from_terminal_echo() -> None:
    command = jt.build_jupyter_exec_command("echo hi", marker="__INSPIRE_DONE_abc__")

    assert command.startswith("echo '")
    assert command.endswith("' | base64 -d | bash\r")
    encoded = command[len("echo '") : -len("' | base64 -d | bash\r")]
    decoded = base64.b64decode(encoded).decode()
    assert "echo hi" in decoded
    assert "__INSPIRE_DONE_abc__" in decoded
    assert "__INSPIRE_DONE_abc__" not in encoded


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


def test_send_terminal_command_capture_collects_stdout_until_marker() -> None:
    captured: dict[str, object] = {}

    class _Frame:
        def evaluate(self, script: str, payload: dict):  # noqa: ANN201
            captured["script"] = script
            captured["payload"] = payload
            return "hello\n__INSPIRE_DONE_abc__:exit:0\n"

    result = jt._send_terminal_command_capture_via_websocket(
        _Frame(),
        ws_url="wss://nb.example.com/terminals/websocket/1",
        stdin_data="echo hi\r",
        timeout_ms=5000,
        marker="__INSPIRE_DONE_abc__",
    )

    assert result is not None
    assert result.returncode == 0
    assert result.output == "hello\n"
    assert captured["payload"] == {
        "wsUrl": "wss://nb.example.com/terminals/websocket/1",
        "stdinData": "echo hi\r",
        "timeoutMs": 5000,
        "promptTimeoutMs": 3000,
        "marker": "__INSPIRE_DONE_abc__",
    }
    assert "new WebSocket" in captured["script"]
    assert 'const donePrefix = marker + ":exit:"' in captured["script"]
    assert "/^\\d+\\s/" in captured["script"]


def test_send_terminal_command_capture_returns_none_on_evaluate_failure() -> None:
    class _Frame:
        def evaluate(self, script: str, payload: dict):  # noqa: ANN201
            raise RuntimeError("browser closed")

    assert (
        jt._send_terminal_command_capture_via_websocket(
            _Frame(),
            ws_url="wss://nb.example.com/terminals/websocket/1",
            stdin_data="echo hi\r",
            timeout_ms=5000,
            marker="__INSPIRE_DONE_abc__",
        )
        is None
    )


def test_run_command_capture_in_existing_lab_cleans_up_terminal(monkeypatch) -> None:  # noqa: ANN001
    events: list[tuple[str, object]] = []

    class _Frame:
        url = "https://nb.example.com/lab"

    monkeypatch.setattr(jt.rtunnel_module, "_create_terminal_via_api", lambda *_a, **_k: "term-1")
    monkeypatch.setattr(
        jt.rtunnel_module,
        "_build_terminal_websocket_url",
        lambda _url, _term: "wss://nb.example.com/terminals/websocket/term-1",
    )
    monkeypatch.setattr(
        jt,
        "_send_terminal_command_capture_via_websocket",
        lambda *_a, **_k: jt.JupyterCommandResult(
            returncode=0,
            output="ok\n",
            completed=True,
            marker=_k["marker"],
        ),
    )
    monkeypatch.setattr(
        jt.rtunnel_module,
        "_delete_terminal_via_api",
        lambda _ctx, *, lab_url, term_name: events.append(("delete", f"{lab_url}|{term_name}"))
        or True,
    )

    result = jt.run_command_capture_in_existing_lab(
        context=object(),
        lab_frame=_Frame(),
        command="echo ok",
        timeout_ms=5000,
    )

    assert result.returncode == 0
    assert result.output == "ok\n"
    assert events == [("delete", "https://nb.example.com/lab|term-1")]



def test_capture_uses_direct_lab_timeout_and_target_account(monkeypatch) -> None:  # noqa: ANN001
    calls: dict[str, object] = {}

    class _Page:
        pass

    class _Context:
        def __init__(self) -> None:
            self.page = _Page()

        def new_page(self) -> _Page:
            return self.page

        def close(self) -> None:
            calls["context_closed"] = True

    class _Browser:
        def close(self) -> None:
            calls["browser_closed"] = True

    class _SyncPlaywright:
        def __enter__(self):  # noqa: ANN201
            return object()

        def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN201
            return False

    fake_sync_api = ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: _SyncPlaywright()
    fake_playwright = ModuleType("playwright")
    fake_playwright.sync_api = fake_sync_api
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    context = _Context()
    browser = _Browser()
    session = SimpleNamespace(
        storage_state={"cookies": []},
        account="secondary",
        base_url="https://secondary.example.test",
    )
    expected = jt.JupyterCommandResult(
        returncode=0,
        output="ok\n",
        completed=True,
        marker="done",
    )

    def fake_launch(_p, *, headless, account):  # noqa: ANN001, ANN202
        calls["launch"] = {"headless": headless, "account": account}
        return browser

    def fake_context(_browser, *, storage_state, account):  # noqa: ANN001, ANN202
        calls["context"] = {"storage_state": storage_state, "account": account}
        return context

    def fake_open(_page, **kwargs):  # noqa: ANN001, ANN202
        calls["open"] = kwargs
        return SimpleNamespace(url="https://secondary.example.test/api/v1/notebook/lab/nb-123")

    monkeypatch.setattr(jt, "_launch_browser", fake_launch)
    monkeypatch.setattr(jt, "_new_context", fake_context)
    monkeypatch.setattr(jt, "open_notebook_lab", fake_open)
    monkeypatch.setattr(jt, "run_command_capture_in_existing_lab", lambda **_k: expected)

    result = jt._run_command_capture_in_notebook_sync(
        notebook_id="nb-123",
        command="echo ok",
        session=session,
        headless=True,
        timeout=9,
        marker="done",
    )

    assert result is expected
    assert calls["launch"] == {"headless": True, "account": "secondary"}
    assert calls["context"] == {
        "storage_state": {"cookies": []},
        "account": "secondary",
    }
    assert calls["open"] == {
        "notebook_id": "nb-123",
        "timeout": 9000,
        "session": session,
        "prefer_direct": True,
    }
    assert calls["context_closed"] is True
    assert calls["browser_closed"] is True



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
