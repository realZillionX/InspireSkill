"""Tests for notebook Jupyter URL helpers."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from inspire.platform.web.browser_api import rtunnel as rtunnel_module
from inspire.platform.web.browser_api import playwright_notebooks as notebooks_module
from inspire.platform.web.browser_api.playwright_notebooks import build_jupyter_proxy_url


def test_build_jupyter_proxy_url_includes_token_from_path() -> None:
    lab_url = (
        "https://nat-notebook-inspire.sii.edu.cn/ws-xxx/project-yyy/user-zzz/"
        "jupyter/notebook-123/token-abc/lab"
    )

    proxy_url = build_jupyter_proxy_url(lab_url, port=31337)

    assert proxy_url.endswith("/proxy/31337/?token=token-abc")


def test_build_jupyter_proxy_url_prefers_query_token() -> None:
    lab_url = (
        "https://nat-notebook-inspire.sii.edu.cn/ws-xxx/project-yyy/user-zzz/"
        "jupyter/notebook-123/token-abc/lab?token=query-token"
    )

    proxy_url = build_jupyter_proxy_url(lab_url, port=31337)

    assert proxy_url.endswith("/proxy/31337/?token=query-token")


def test_build_jupyter_proxy_url_notebook_lab_pattern() -> None:
    lab_url = "https://qz.sii.edu.cn/api/v1/notebook/lab/notebook-123/"

    proxy_url = build_jupyter_proxy_url(lab_url, port=31337)

    assert proxy_url == "https://qz.sii.edu.cn/api/v1/notebook/lab/notebook-123/proxy/31337/"


class _FakeFrame:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakePage:
    def __init__(self, fake_time: list[float]) -> None:
        self._fake_time = fake_time
        self.goto_calls: list[str] = []
        self.wait_calls = 0
        self._frames: list[_FakeFrame] = []
        self.url = ""

    @property
    def frames(self) -> list[_FakeFrame]:
        return self._frames

    def goto(self, url: str, timeout: int, wait_until: str) -> None:
        assert timeout > 0
        assert wait_until == "domcontentloaded"
        self.goto_calls.append(url)
        self.url = url
        if "/ide?notebook_id=" in url:
            self._frames = []
        elif "/api/v1/notebook/lab/" in url:
            self._frames = [_FakeFrame(url)]

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_calls += 1
        self._fake_time[0] += timeout_ms / 1000.0


def test_open_notebook_lab_falls_back_early_to_direct_url(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake_time = [0.0]
    page = _FakePage(fake_time)
    monkeypatch.setattr(notebooks_module, "_get_base_url", lambda: "https://qz.sii.edu.cn")
    monkeypatch.setattr(notebooks_module, "_browser_api_path", lambda path: f"/api/v1{path}")
    monkeypatch.setattr(notebooks_module.time, "time", lambda: fake_time[0])

    lab = notebooks_module.open_notebook_lab(page, notebook_id="nb-123", timeout=60000)

    assert lab is not None
    assert len(page.goto_calls) == 2
    assert page.goto_calls[0] == "https://qz.sii.edu.cn/ide?notebook_id=nb-123"
    assert page.goto_calls[1] == "https://qz.sii.edu.cn/api/v1/notebook/lab/nb-123/"
    assert fake_time[0] < 20.0


def test_send_command_via_terminal_ws_cleans_up_terminal(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    events: list[tuple[str, object]] = []

    class _Frame:
        url = "https://nb.example.com/lab"

    monkeypatch.setattr(rtunnel_module, "_create_terminal_via_api", lambda *_a, **_k: "term-1")
    monkeypatch.setattr(
        rtunnel_module,
        "_build_terminal_websocket_url",
        lambda _url, _term: "wss://nb.example.com/terminals/websocket/term-1",
    )

    def fake_send(_frame, **kwargs):  # noqa: ANN001, ANN202
        events.append(("send", kwargs))
        return True

    monkeypatch.setattr(rtunnel_module, "_send_terminal_command_via_websocket", fake_send)
    monkeypatch.setattr(
        rtunnel_module,
        "_delete_terminal_via_api",
        lambda _ctx, *, lab_url, term_name: events.append(("delete", f"{lab_url}|{term_name}"))
        or True,
    )

    assert (
        notebooks_module._send_command_via_terminal_ws(
            context=object(),
            lab_frame=_Frame(),
            command="echo hi",
            timeout_ms=1234,
            completion_marker="DONE",
        )
        is True
    )
    assert events[0][0] == "send"
    assert events[0][1]["completion_marker"] == "DONE"
    assert events[1] == ("delete", "https://nb.example.com/lab|term-1")


def test_run_command_in_notebook_sync_falls_back_to_browser_terminal(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    waits: list[int] = []

    class _LoadingLocator:
        @property
        def first(self):  # noqa: ANN201
            return self

        def wait_for(self, **_kwargs) -> None:  # noqa: ANN003
            return None

    class _Frame:
        url = "https://nb.example.com/lab"

        def __init__(self) -> None:
            self.marker_checks = 0

        def locator(self, _selector: str) -> _LoadingLocator:
            return _LoadingLocator()

        def evaluate(self, _script: str, marker: str) -> bool:
            self.marker_checks += 1
            return (
                marker.startswith(notebooks_module.COMMAND_COMPLETION_MARKER_PREFIX)
                and self.marker_checks >= 2
            )

        def wait_for_timeout(self, timeout_ms: int) -> None:
            waits.append(timeout_ms)

    class _Keyboard:
        def __init__(self) -> None:
            self.inserted: list[str] = []
            self.pressed: list[str] = []

        def insert_text(self, text: str) -> None:
            self.inserted.append(text)

        def press(self, key: str) -> None:
            self.pressed.append(key)

    class _Page:
        def __init__(self) -> None:
            self.keyboard = _Keyboard()

        def wait_for_timeout(self, timeout_ms: int) -> None:
            waits.append(timeout_ms)

    class _Context:
        def __init__(self, page: _Page) -> None:
            self._page = page
            self.closed = False

        def new_page(self) -> _Page:
            return self._page

        def close(self) -> None:
            self.closed = True

    class _Browser:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

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

    page = _Page()
    context = _Context(page)
    browser = _Browser()
    frame = _Frame()

    monkeypatch.setattr(notebooks_module, "_launch_browser", lambda *_a, **_k: browser)
    monkeypatch.setattr(notebooks_module, "_new_context", lambda *_a, **_k: context)
    monkeypatch.setattr(notebooks_module, "open_notebook_lab", lambda *_a, **_k: frame)
    monkeypatch.setattr(notebooks_module, "_send_command_via_terminal_ws", lambda **_k: False)
    monkeypatch.setattr(
        rtunnel_module, "_open_or_create_terminal", lambda *_a, **_k: (True, "term-1")
    )
    monkeypatch.setattr(rtunnel_module, "_focus_terminal_input", lambda *_a, **_k: True)

    assert (
        notebooks_module._run_command_in_notebook_sync(
            notebook_id="nb-123",
            command="echo hi",
            session=SimpleNamespace(storage_state={}),
            timeout=7,
        )
        is True
    )
    assert len(page.keyboard.inserted) == 1
    assert "echo hi" in page.keyboard.inserted[0]
    assert notebooks_module.COMMAND_COMPLETION_MARKER_PREFIX in page.keyboard.inserted[0]
    assert page.keyboard.pressed == ["Enter"]
    assert waits == [250]
    assert context.closed is True
    assert browser.closed is True
