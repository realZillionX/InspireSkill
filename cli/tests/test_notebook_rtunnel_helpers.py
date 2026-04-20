"""Tests for REST API terminal creation, batch script, and _StepTimer helpers."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from inspire.platform.web.browser_api import rtunnel as rtunnel_module
from inspire.platform.web.browser_api.rtunnel import (
    _CONTENTS_API_RTUNNEL_FILENAME,
    SETUP_DONE_MARKER,
    _StepTimer,
    _build_batch_setup_script,
    _compute_rtunnel_hash,
    _build_terminal_websocket_url,
    _create_terminal_via_api,
    _delete_terminal_via_api,
    _download_rtunnel_locally,
    _extract_jupyter_token,
    _focus_terminal_input,
    _jupyter_server_base,
    _open_or_create_terminal,
    _resolve_rtunnel_binary,
    _rtunnel_matches_on_notebook,
    _send_setup_command_via_terminal_ws,
    _send_terminal_command_via_websocket,
    _upload_rtunnel_via_contents_api,
    _upload_rtunnel_hash_sidecar,
    _verify_terminal_focus,
    _wait_for_setup_completion,
    _wait_for_terminal_surface,
    _wait_for_terminal_surface_progressive,
)


# ---------------------------------------------------------------------------
# _jupyter_server_base
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lab_url", "expected"),
    [
        # Standard: lab URL with /lab suffix
        (
            "https://notebook-inspire.example.com/lab",
            "https://notebook-inspire.example.com/",
        ),
        (
            "https://notebook-inspire.example.com/lab/",
            "https://notebook-inspire.example.com/",
        ),
        # Proxy-style: /notebook/lab/<id>/lab (JupyterLab route is the final /lab)
        (
            "https://example.com/api/v1/notebook/lab/nb-123/lab",
            "https://example.com/api/v1/notebook/lab/nb-123/",
        ),
        # Direct navigation URL (no /lab suffix) — no stripping
        (
            "https://example.com/api/v1/notebook/lab/nb-123/",
            "https://example.com/api/v1/notebook/lab/nb-123/",
        ),
        # Query parameters and fragments are stripped
        (
            "https://example.com/lab?token=abc#foo",
            "https://example.com/",
        ),
    ],
)
def test_jupyter_server_base(lab_url: str, expected: str) -> None:
    assert _jupyter_server_base(lab_url) == expected


# ---------------------------------------------------------------------------
# _create_terminal_via_api
# ---------------------------------------------------------------------------


class _DummyResponse:
    def __init__(self, status: int, data: dict | None = None) -> None:
        self.status = status
        self._data = data

    def json(self) -> dict:
        return self._data or {}


class _DummyRequest:
    def __init__(self, response: _DummyResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, int]] = []

    def post(self, url: str, headers: dict | None = None, timeout: int = 0) -> _DummyResponse:
        self.calls.append((url, timeout))
        return self._response


class _DummyContext:
    def __init__(self, request: _DummyRequest) -> None:
        self.request = request

    def cookies(self) -> list[dict]:
        return []


def test_create_terminal_via_api_success() -> None:
    resp = _DummyResponse(200, {"name": "3"})
    ctx = _DummyContext(_DummyRequest(resp))
    result = _create_terminal_via_api(ctx, "https://nb.example.com/lab")
    assert result == "3"
    assert len(ctx.request.calls) == 1
    assert ctx.request.calls[0][0] == "https://nb.example.com/api/terminals"


def test_create_terminal_via_api_201() -> None:
    resp = _DummyResponse(201, {"name": "1"})
    ctx = _DummyContext(_DummyRequest(resp))
    result = _create_terminal_via_api(ctx, "https://nb.example.com/lab/")
    assert result == "1"


def test_create_terminal_via_api_failure_status() -> None:
    resp = _DummyResponse(403, {})
    ctx = _DummyContext(_DummyRequest(resp))
    result = _create_terminal_via_api(ctx, "https://nb.example.com/lab")
    assert result is None


def test_create_terminal_via_api_exception() -> None:
    class _BrokenRequest:
        def post(self, url: str, headers: dict | None = None, timeout: int = 0) -> None:
            raise ConnectionError("network failure")

    ctx = _DummyContext(_BrokenRequest())  # type: ignore[arg-type]
    result = _create_terminal_via_api(ctx, "https://nb.example.com/lab")
    assert result is None


def test_create_terminal_via_api_playwright_exception() -> None:
    class _BrokenRequest:
        def post(self, url: str, headers: dict | None = None, timeout: int = 0) -> None:
            raise rtunnel_module.PlaywrightError("request failed")

    ctx = _DummyContext(_BrokenRequest())  # type: ignore[arg-type]
    result = _create_terminal_via_api(ctx, "https://nb.example.com/lab")
    assert result is None


def test_create_terminal_via_api_proxy_url() -> None:
    """API URL should be derived from the server base, not the lab path."""
    resp = _DummyResponse(200, {"name": "2"})
    ctx = _DummyContext(_DummyRequest(resp))
    result = _create_terminal_via_api(ctx, "https://example.com/api/v1/notebook/lab/nb-123/lab")
    assert result == "2"
    assert ctx.request.calls[0][0] == "https://example.com/api/v1/notebook/lab/nb-123/api/terminals"


# ---------------------------------------------------------------------------
# _delete_terminal_via_api
# ---------------------------------------------------------------------------


class _DummyDeleteRequest:
    def __init__(self, status: int) -> None:
        self.status = status
        self.calls: list[tuple[str, dict | None, int]] = []

    def delete(self, url: str, headers: dict | None = None, timeout: int = 0) -> _DummyResponse:
        self.calls.append((url, headers, timeout))
        return _DummyResponse(self.status, {})


class _DummyDeleteContext:
    def __init__(self, request: _DummyDeleteRequest, cookies: list[dict] | None = None) -> None:
        self.request = request
        self._cookies = cookies or []

    def cookies(self) -> list[dict]:
        return self._cookies


def test_delete_terminal_via_api_success_with_xsrf_header() -> None:
    request = _DummyDeleteRequest(status=204)
    ctx = _DummyDeleteContext(
        request,
        cookies=[{"name": "_xsrf", "value": "token-123"}],
    )

    assert (
        _delete_terminal_via_api(ctx, lab_url="https://nb.example.com/lab", term_name="7") is True
    )
    assert len(request.calls) == 1
    assert request.calls[0][0] == "https://nb.example.com/api/terminals/7"
    assert request.calls[0][1] == {"X-XSRFToken": "token-123"}


def test_delete_terminal_via_api_404_is_treated_as_success() -> None:
    request = _DummyDeleteRequest(status=404)
    ctx = _DummyDeleteContext(request)

    assert (
        _delete_terminal_via_api(ctx, lab_url="https://nb.example.com/lab", term_name="7") is True
    )


def test_delete_terminal_via_api_failure_status() -> None:
    request = _DummyDeleteRequest(status=500)
    ctx = _DummyDeleteContext(request)

    assert (
        _delete_terminal_via_api(ctx, lab_url="https://nb.example.com/lab", term_name="7") is False
    )


# ---------------------------------------------------------------------------
# websocket url/token helpers
# ---------------------------------------------------------------------------


def test_extract_jupyter_token_prefers_query_token() -> None:
    lab_url = "https://example.com/jupyter/nb/path-token/lab?token=query-token"
    assert _extract_jupyter_token(lab_url) == "query-token"


def test_extract_jupyter_token_from_path() -> None:
    lab_url = "https://example.com/jupyter/nb-123/path-token/lab"
    assert _extract_jupyter_token(lab_url) == "path-token"


def test_extract_jupyter_token_missing() -> None:
    lab_url = "https://example.com/api/v1/notebook/lab/nb-123/"
    assert _extract_jupyter_token(lab_url) is None


def test_build_terminal_websocket_url_https() -> None:
    lab_url = "https://example.com/jupyter/nb-123/path-token/lab?token=query-token"
    ws_url = _build_terminal_websocket_url(lab_url, "7")
    assert (
        ws_url
        == "wss://example.com/jupyter/nb-123/path-token/terminals/websocket/7?token=query-token"
    )


def test_build_terminal_websocket_url_http_without_token() -> None:
    lab_url = "http://example.com/api/v1/notebook/lab/nb-123/"
    ws_url = _build_terminal_websocket_url(lab_url, "term-a")
    assert ws_url == "ws://example.com/api/v1/notebook/lab/nb-123/terminals/websocket/term-a"


def test_send_terminal_command_via_websocket_success() -> None:
    captured: dict = {}

    class _EvalPage:
        def evaluate(self, script: str, payload: dict):  # noqa: ANN201
            captured["script"] = script
            captured["payload"] = payload
            return True

    page = _EvalPage()
    result = _send_terminal_command_via_websocket(
        page,
        ws_url="wss://example.test/terminals/websocket/1",
        command="echo hi",
        timeout_ms=1234,
    )

    assert result is True
    assert "WebSocket" in captured["script"]
    assert captured["payload"]["wsUrl"] == "wss://example.test/terminals/websocket/1"
    assert captured["payload"]["stdinData"] == "echo hi\r"
    assert captured["payload"]["timeoutMs"] == 1234
    # promptTimeoutMs = min(1234 - 500, 3000) = 734
    assert captured["payload"]["promptTimeoutMs"] == 734


def test_send_terminal_command_via_websocket_completion_marker() -> None:
    captured: dict = {}

    class _EvalPage:
        def evaluate(self, script: str, payload: dict):  # noqa: ANN201
            captured["script"] = script
            captured["payload"] = payload
            return True

    page = _EvalPage()
    result = _send_terminal_command_via_websocket(
        page,
        ws_url="wss://example.test/terminals/websocket/1",
        command="echo hi",
        timeout_ms=5000,
        completion_marker=SETUP_DONE_MARKER,
    )

    assert result is True
    assert captured["payload"]["marker"] == SETUP_DONE_MARKER
    assert captured["payload"]["timeoutMs"] == 5000


def test_send_terminal_command_via_websocket_exception() -> None:
    class _BrokenPage:
        def evaluate(self, script: str, payload: dict):  # noqa: ANN201
            raise RuntimeError("eval failed")

    page = _BrokenPage()
    result = _send_terminal_command_via_websocket(
        page,
        ws_url="wss://example.test/terminals/websocket/1",
        command="echo hi",
    )
    assert result is False


def test_send_terminal_command_via_websocket_playwright_exception() -> None:
    class _BrokenPage:
        def evaluate(self, script: str, payload: dict):  # noqa: ANN201
            raise rtunnel_module.PlaywrightError("eval failed")

    page = _BrokenPage()
    result = _send_terminal_command_via_websocket(
        page,
        ws_url="wss://example.test/terminals/websocket/1",
        command="echo hi",
    )
    assert result is False


def test_send_setup_command_via_terminal_ws_cleans_up_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str]] = []
    ws_call: dict[str, object] = {}

    class _Frame:
        url = "https://nb.example.com/lab"

    monkeypatch.setattr(rtunnel_module, "_create_terminal_via_api", lambda *_a, **_k: "term-1")
    monkeypatch.setattr(
        rtunnel_module,
        "_build_terminal_websocket_url",
        lambda _url, _term: "wss://nb.example.com/terminals/websocket/term-1",
    )

    def fake_send_terminal_command_via_websocket(*_args, **kwargs):  # type: ignore[no-untyped-def]
        ws_call.update(kwargs)
        events.append(("send", "ok"))
        return True

    monkeypatch.setattr(
        rtunnel_module,
        "_send_terminal_command_via_websocket",
        fake_send_terminal_command_via_websocket,
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_delete_terminal_via_api",
        lambda _ctx, *, lab_url, term_name: events.append(("delete", f"{lab_url}|{term_name}"))
        or True,
    )

    assert (
        _send_setup_command_via_terminal_ws(context=object(), lab_frame=_Frame(), batch_cmd="echo")
        is True
    )
    assert ("send", "ok") in events
    assert ("delete", "https://nb.example.com/lab|term-1") in events
    assert ws_call["timeout_ms"] == 120000
    assert ws_call["completion_marker"] == SETUP_DONE_MARKER


def test_send_setup_command_via_terminal_ws_cleans_up_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _Frame:
        url = "https://nb.example.com/lab"

    monkeypatch.setattr(rtunnel_module, "_create_terminal_via_api", lambda *_a, **_k: "term-2")
    monkeypatch.setattr(
        rtunnel_module,
        "_build_terminal_websocket_url",
        lambda _url, _term: "wss://nb.example.com/terminals/websocket/term-2",
    )
    monkeypatch.setattr(
        rtunnel_module, "_send_terminal_command_via_websocket", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_delete_terminal_via_api",
        lambda *_a, **_k: events.append("deleted") or True,
    )

    assert (
        _send_setup_command_via_terminal_ws(
            context=object(),
            lab_frame=_Frame(),
            batch_cmd="echo",
        )
        is False
    )
    assert events == ["deleted"]


def test_send_setup_command_via_terminal_ws_returns_false_when_terminal_create_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rtunnel_module, "_create_terminal_via_api", lambda *_a, **_k: None)

    assert (
        _send_setup_command_via_terminal_ws(
            context=object(),
            lab_frame=type("_Frame", (), {"url": "https://nb.example.com/lab"})(),
            batch_cmd="echo",
        )
        is False
    )


# ---------------------------------------------------------------------------
# terminal surface and focus helpers
# ---------------------------------------------------------------------------


class _LocatorStub:
    def __init__(
        self,
        *,
        count: int = 0,
        wait_ok: bool = False,
        visible: bool = False,
    ) -> None:
        self._count = count
        self._wait_ok = wait_ok
        self._visible = visible
        self.first = self
        self.wait_calls: list[tuple[str, int]] = []
        self.click_calls: list[int] = []

    def count(self) -> int:
        return self._count

    def is_visible(self, timeout: int = 0) -> bool:
        return self._visible

    def wait_for(self, *, state: str, timeout: int) -> None:
        self.wait_calls.append((state, timeout))
        if not self._wait_ok:
            raise TimeoutError("not ready")

    def click(self, timeout: int = 0, force: bool = False) -> None:
        self.click_calls.append(timeout)


class _FrameStub:
    def __init__(
        self,
        selectors: dict[str, _LocatorStub],
        evaluate_results: list[object] | None = None,
    ) -> None:
        self._selectors = selectors
        self._evaluate_results = list(evaluate_results) if evaluate_results else []
        self._evaluate_idx = 0

    def locator(self, selector: str) -> _LocatorStub:
        return self._selectors.setdefault(selector, _LocatorStub())

    def evaluate(self, expression: str) -> object:
        if self._evaluate_idx < len(self._evaluate_results):
            result = self._evaluate_results[self._evaluate_idx]
            self._evaluate_idx += 1
            return result
        return None


class _PageStub:
    def __init__(self) -> None:
        self.wait_calls: list[int] = []

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_calls.append(timeout_ms)


def test_wait_for_terminal_surface_uses_xterm_wait() -> None:
    xterm = _LocatorStub(wait_ok=True)
    frame = _FrameStub({".xterm": xterm})

    assert _wait_for_terminal_surface(frame, timeout_ms=1234) is True
    assert xterm.wait_calls == [("attached", 1234)]


def test_wait_for_terminal_surface_falls_back_to_textarea_count() -> None:
    frame = _FrameStub(
        {
            ".xterm": _LocatorStub(wait_ok=False),
            "textarea.xterm-helper-textarea": _LocatorStub(count=1),
        }
    )

    assert _wait_for_terminal_surface(frame, timeout_ms=500) is True


def test_wait_for_terminal_surface_progressive_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_time = [0.0]
    attempts = {"count": 0}

    def fake_monotonic() -> float:
        return fake_time[0]

    def fake_wait_surface(_frame, *, timeout_ms: int) -> bool:  # type: ignore[no-untyped-def]
        assert timeout_ms > 0
        attempts["count"] += 1
        return attempts["count"] >= 3

    monkeypatch.setattr(rtunnel_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(rtunnel_module, "_wait_for_terminal_surface", fake_wait_surface)
    monkeypatch.setattr(rtunnel_module, "_click_terminal_tab", lambda *_args, **_kwargs: False)

    class _ProgressPage:
        def __init__(self, now_ref: list[float]) -> None:
            self.now_ref = now_ref
            self.wait_calls: list[int] = []

        def wait_for_timeout(self, timeout_ms: int) -> None:
            self.wait_calls.append(timeout_ms)
            self.now_ref[0] += timeout_ms / 1000.0

    page = _ProgressPage(fake_time)
    frame = _FrameStub({})

    assert (
        _wait_for_terminal_surface_progressive(
            frame,
            page,
            total_timeout_ms=1200,
            poll_ms=200,
            tab_poke_interval_ms=500,
        )
        is True
    )
    assert attempts["count"] >= 3
    assert page.wait_calls


def test_wait_for_terminal_surface_progressive_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_time = [0.0]

    def fake_monotonic() -> float:
        return fake_time[0]

    monkeypatch.setattr(rtunnel_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(
        rtunnel_module, "_wait_for_terminal_surface", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(rtunnel_module, "_click_terminal_tab", lambda *_args, **_kwargs: False)

    class _ProgressPage:
        def __init__(self, now_ref: list[float]) -> None:
            self.now_ref = now_ref
            self.wait_calls: list[int] = []

        def wait_for_timeout(self, timeout_ms: int) -> None:
            self.wait_calls.append(timeout_ms)
            self.now_ref[0] += timeout_ms / 1000.0

    page = _ProgressPage(fake_time)
    frame = _FrameStub({})

    assert (
        _wait_for_terminal_surface_progressive(
            frame,
            page,
            total_timeout_ms=700,
            poll_ms=150,
            tab_poke_interval_ms=300,
        )
        is False
    )
    assert page.wait_calls
    assert fake_time[0] > 0.0


def test_verify_terminal_focus_true() -> None:
    frame = _FrameStub({}, evaluate_results=["textarea", "xterm-helper-textarea"])
    assert _verify_terminal_focus(frame) is True


def test_verify_terminal_focus_wrong_tag() -> None:
    frame = _FrameStub({}, evaluate_results=["div", "xterm-helper-textarea"])
    assert _verify_terminal_focus(frame) is False


def test_verify_terminal_focus_wrong_class() -> None:
    frame = _FrameStub({}, evaluate_results=["textarea", "some-other-class"])
    assert _verify_terminal_focus(frame) is False


def test_verify_terminal_focus_exception() -> None:
    class _BrokenFrame:
        def evaluate(self, _expr: str) -> object:
            raise RuntimeError("frame detached")

    assert _verify_terminal_focus(_BrokenFrame()) is False


def test_focus_terminal_input_succeeds_via_xterm_container() -> None:
    """Focus via .xterm container click when it's visible and focus verifies."""
    xterm = _LocatorStub(count=1, visible=True)
    textarea = _LocatorStub(wait_ok=True)
    # evaluate returns: tagName="textarea", className="xterm-helper-textarea"
    frame = _FrameStub(
        {".xterm": xterm, "textarea.xterm-helper-textarea": textarea},
        evaluate_results=["textarea", "xterm-helper-textarea"],
    )
    page = _PageStub()

    assert _focus_terminal_input(frame, page) is True
    assert len(xterm.click_calls) == 1
    assert 40 in page.wait_calls


def test_focus_terminal_input_succeeds_via_force_click_textarea() -> None:
    """When .xterm click doesn't verify focus, atomic JS focus path succeeds."""
    # .xterm verify fails (2 evaluates), then atomic JS returns True (1 evaluate).
    textarea = _LocatorStub(wait_ok=True)
    frame = _FrameStub(
        {".xterm": _LocatorStub(count=1), "textarea.xterm-helper-textarea": textarea},
        evaluate_results=["div", "", True],
    )
    page = _PageStub()

    assert _focus_terminal_input(frame, page) is True


def test_focus_terminal_input_returns_false_when_focus_never_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returns False when both focus strategies fail on all passes."""
    monkeypatch.setattr(rtunnel_module, "_click_terminal_tab", lambda *_a, **_kw: False)

    # Per pass: .xterm verify consumes 2 evaluates, atomic JS consumes 1.
    # "div", "" → verify fails; False → atomic JS returns falsy.
    textarea = _LocatorStub(wait_ok=True)
    frame = _FrameStub(
        {".xterm": _LocatorStub(count=1), "textarea.xterm-helper-textarea": textarea},
        evaluate_results=["div", "", False] * 5,
    )
    page = _PageStub()

    assert _focus_terminal_input(frame, page) is False


def test_focus_terminal_input_succeeds_via_atomic_js_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Atomic JS focus succeeds when .xterm click path fails."""
    monkeypatch.setattr(rtunnel_module, "_click_terminal_tab", lambda *_a, **_kw: False)

    # .xterm count=0 (Try 1 skipped), atomic JS returns True
    textarea = _LocatorStub(wait_ok=True)
    frame = _FrameStub(
        {".xterm": _LocatorStub(count=0), "textarea.xterm-helper-textarea": textarea},
        evaluate_results=[True],
    )
    page = _PageStub()

    assert _focus_terminal_input(frame, page) is True


def test_focus_terminal_input_returns_false_when_textarea_not_attached() -> None:
    """Returns False immediately when xterm textarea hasn't been created yet."""
    textarea = _LocatorStub(wait_ok=False)  # textarea not yet attached
    xterm = _LocatorStub(count=1, wait_ok=True)
    frame = _FrameStub(
        {".xterm": xterm, "textarea.xterm-helper-textarea": textarea},
        evaluate_results=["textarea", "xterm-helper-textarea"],
    )
    page = _PageStub()

    assert _focus_terminal_input(frame, page) is False
    # Should not have attempted any clicks (gate failed before retry loop)
    assert len(xterm.click_calls) == 0


def test_focus_terminal_input_returns_false_when_unavailable() -> None:
    frame = _FrameStub({})
    page = _PageStub()

    assert _focus_terminal_input(frame, page) is False


def test_open_or_create_terminal_returns_early_when_api_path_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, int] = {"recover": 0, "entry": 0, "fallback": 0}

    monkeypatch.setattr(
        rtunnel_module,
        "_open_terminal_via_rest_api",
        lambda **_kwargs: (True, True, "api-1"),
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_recover_api_terminal_surface",
        lambda **_kwargs: calls.__setitem__("recover", calls["recover"] + 1) or False,
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_wait_for_terminal_entry_point",
        lambda **_kwargs: calls.__setitem__("entry", calls["entry"] + 1),
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_open_terminal_via_dom_fallback",
        lambda **_kwargs: calls.__setitem__("fallback", calls["fallback"] + 1) or True,
    )

    result, term_name = _open_or_create_terminal(
        context=object(), page=object(), lab_frame=object()
    )
    assert result is True
    assert term_name == "api-1"
    assert calls["recover"] == 0
    assert calls["entry"] == 0
    assert calls["fallback"] == 0


def test_open_or_create_terminal_uses_dom_fallback_after_api_recovery_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    monkeypatch.setattr(
        rtunnel_module,
        "_open_terminal_via_rest_api",
        lambda **_kwargs: (False, True, "api-2"),
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_recover_api_terminal_surface",
        lambda **_kwargs: False,
    )

    def fake_wait_entry(*, lab_frame, api_term_created: bool) -> None:  # noqa: ANN001
        events.append(("entry", api_term_created))

    monkeypatch.setattr(rtunnel_module, "_wait_for_terminal_entry_point", fake_wait_entry)
    monkeypatch.setattr(
        rtunnel_module,
        "_dismiss_terminal_dialog_once",
        lambda **kwargs: events.append(("dismiss", kwargs["settle_ms"])) or False,
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_open_terminal_via_dom_fallback",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_click_terminal_tab",
        lambda *_args, **kwargs: events.append(("tab_click", kwargs["settle_ms"])) or True,
    )

    result, term_name = _open_or_create_terminal(
        context=object(), page=object(), lab_frame=object()
    )
    assert result is True
    assert term_name == "api-2"
    assert ("entry", True) in events
    assert ("tab_click", 80) in events


def test_open_or_create_terminal_handles_api_full_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    monkeypatch.setattr(
        rtunnel_module, "_open_terminal_via_rest_api", lambda **_kwargs: (False, False, None)
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_wait_for_terminal_entry_point",
        lambda **kwargs: events.append(("entry", kwargs["api_term_created"])),
    )
    monkeypatch.setattr(rtunnel_module, "_dismiss_terminal_dialog_once", lambda **_kwargs: False)
    monkeypatch.setattr(
        rtunnel_module,
        "_open_terminal_via_dom_fallback",
        lambda **kwargs: events.append(("fallback", kwargs["api_term_created"])) or True,
    )

    result, term_name = _open_or_create_terminal(
        context=object(), page=object(), lab_frame=object()
    )
    assert result is True
    assert term_name is None
    assert ("entry", False) in events
    assert ("fallback", False) in events


def test_open_or_create_terminal_returns_false_when_dom_fallback_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"tab_click": 0}

    monkeypatch.setattr(
        rtunnel_module, "_open_terminal_via_rest_api", lambda **_kwargs: (False, False, None)
    )
    monkeypatch.setattr(rtunnel_module, "_wait_for_terminal_entry_point", lambda **_kwargs: None)
    monkeypatch.setattr(rtunnel_module, "_dismiss_terminal_dialog_once", lambda **_kwargs: False)
    monkeypatch.setattr(rtunnel_module, "_open_terminal_via_dom_fallback", lambda **_kwargs: False)
    monkeypatch.setattr(
        rtunnel_module,
        "_click_terminal_tab",
        lambda *_args, **_kwargs: calls.__setitem__("tab_click", calls["tab_click"] + 1) or True,
    )

    result, term_name = _open_or_create_terminal(
        context=object(), page=object(), lab_frame=object()
    )
    assert result is False
    assert term_name is None
    assert calls["tab_click"] == 0


def test_open_or_create_terminal_returns_true_when_api_recovery_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"entry": 0, "fallback": 0}

    monkeypatch.setattr(
        rtunnel_module, "_open_terminal_via_rest_api", lambda **_kwargs: (False, True, "api-5")
    )
    monkeypatch.setattr(rtunnel_module, "_recover_api_terminal_surface", lambda **_kwargs: True)
    monkeypatch.setattr(
        rtunnel_module,
        "_wait_for_terminal_entry_point",
        lambda **_kwargs: calls.__setitem__("entry", calls["entry"] + 1),
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_open_terminal_via_dom_fallback",
        lambda **_kwargs: calls.__setitem__("fallback", calls["fallback"] + 1) or True,
    )

    result, term_name = _open_or_create_terminal(
        context=object(), page=object(), lab_frame=object()
    )
    assert result is True
    assert term_name == "api-5"
    assert calls["entry"] == 0
    assert calls["fallback"] == 0


def test_open_terminal_via_rest_api_handles_playwright_navigation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rtunnel_module, "_create_terminal_via_api", lambda *_args, **_kwargs: "1")

    class _Frame:
        url = "https://nb.example.com/lab"

        def goto(self, *_args, **_kwargs) -> None:
            raise rtunnel_module.PlaywrightError("navigation failed")

    terminal_ready, api_term_created, term_name = (
        rtunnel_module._open_terminal_via_rest_api(  # noqa: SLF001
            context=object(),
            page=object(),
            lab_frame=_Frame(),
        )
    )
    assert terminal_ready is False
    assert api_term_created is True
    assert term_name == "1"


def test_recover_api_terminal_surface_waits_for_menu_before_file_menu_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, int] = {"file_menu": 0}

    monkeypatch.setattr(rtunnel_module, "_click_terminal_tab", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        rtunnel_module,
        "_wait_for_terminal_surface_progressive",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        rtunnel_module, "_wait_for_file_menu_ready", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_open_terminal_from_file_menu",
        lambda *_args, **_kwargs: calls.__setitem__("file_menu", calls["file_menu"] + 1) or True,
    )

    assert (
        rtunnel_module._recover_api_terminal_surface(  # noqa: SLF001
            lab_frame=object(),
            page=object(),
        )
        is False
    )
    assert calls["file_menu"] == 0


# ---------------------------------------------------------------------------
# _build_batch_setup_script
# ---------------------------------------------------------------------------


def test_build_batch_setup_script_roundtrip() -> None:
    commands = [
        "PORT=31337",
        "SSH_PORT=22222",
        "mkdir -p /root/.ssh && chmod 700 /root/.ssh",
        'echo "INSPIRE_RTUNNEL_SETUP_DONE"',
    ]
    result = _build_batch_setup_script(commands)

    # Must be a single line
    assert "\n" not in result

    # Must start with echo and end with bash
    assert result.startswith("echo '")
    assert result.endswith("' | base64 -d | bash")

    # Extract and decode the base64 payload
    b64_payload = result[len("echo '") : result.index("' | base64 -d | bash")]
    decoded = base64.b64decode(b64_payload).decode()

    # Decoded script should contain all original commands
    for cmd in commands:
        assert cmd in decoded

    # Lines should be newline-separated
    lines = decoded.strip().split("\n")
    assert lines == commands


def test_build_batch_setup_script_empty() -> None:
    result = _build_batch_setup_script([])
    assert result.startswith("echo '")
    b64_payload = result[len("echo '") : result.index("' | base64 -d | bash")]
    decoded = base64.b64decode(b64_payload).decode()
    assert decoded == "\n"


# ---------------------------------------------------------------------------
# _wait_for_setup_completion
# ---------------------------------------------------------------------------


class _TimerStub:
    def __init__(self) -> None:
        self.labels: list[str] = []

    def mark(self, label: str) -> None:
        self.labels.append(label)


class _WaitPageStub:
    def __init__(self) -> None:
        self.wait_calls: list[int] = []

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_calls.append(timeout_ms)


def test_wait_for_setup_completion_uses_short_settle_for_ws_path() -> None:
    page = _WaitPageStub()
    timer = _TimerStub()

    _wait_for_setup_completion(page=page, setup_sent_via_ws=True, timer=timer)

    assert page.wait_calls == [500]
    assert timer.labels == ["wait_marker"]


def test_wait_for_setup_completion_uses_longer_settle_for_browser_path() -> None:
    page = _WaitPageStub()
    timer = _TimerStub()

    _wait_for_setup_completion(page=page, setup_sent_via_ws=False, timer=timer)

    assert page.wait_calls == [3000]
    assert timer.labels == ["wait_marker"]


# ---------------------------------------------------------------------------
# _StepTimer
# ---------------------------------------------------------------------------


def test_step_timer_disabled_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    timer = _StepTimer(enabled=False)
    timer.mark("a")
    timer.mark("b")
    timer.summary()
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_step_timer_mark_returns_elapsed() -> None:
    timer = _StepTimer(enabled=False)
    result = timer.mark("x")
    assert result == 0.0
    assert isinstance(result, float)


def test_step_timer_records_steps(capsys: pytest.CaptureFixture[str]) -> None:
    timer = _StepTimer(enabled=True)
    timer.mark("alpha")
    timer.mark("beta")
    captured = capsys.readouterr()
    assert "[timing] alpha:" in captured.err
    assert "[timing] beta:" in captured.err


def test_step_timer_summary_format(capsys: pytest.CaptureFixture[str]) -> None:
    timer = _StepTimer(enabled=True)
    timer.mark("step_one")
    timer.mark("step_two")
    _ = capsys.readouterr()  # discard mark output

    timer.summary()
    captured = capsys.readouterr()
    assert "step_one" in captured.err
    assert "step_two" in captured.err
    assert "%" in captured.err
    assert "TOTAL" in captured.err


def test_step_timer_summary_empty_when_no_steps(
    capsys: pytest.CaptureFixture[str],
) -> None:
    timer = _StepTimer(enabled=True)
    timer.summary()
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


# ---------------------------------------------------------------------------
# _upload_rtunnel_via_contents_api
# ---------------------------------------------------------------------------


class _DummyUploadResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class _DummyUploadRequest:
    def __init__(self, response: _DummyUploadResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, dict | None, dict | None, int]] = []

    def put(
        self,
        url: str,
        headers: dict | None = None,
        data: dict | None = None,
        timeout: int = 0,
    ) -> _DummyUploadResponse:
        self.calls.append((url, headers, data, timeout))
        return self._response


class _DummyUploadContext:
    def __init__(self, request: _DummyUploadRequest) -> None:
        self.request = request

    def cookies(self) -> list[dict]:
        return []


def test_upload_rtunnel_via_contents_api_success(tmp_path: Path) -> None:
    binary = tmp_path / "rtunnel"
    binary.write_bytes(b"\x7fELF_test_binary")

    resp = _DummyUploadResponse(201)
    req = _DummyUploadRequest(resp)
    ctx = _DummyUploadContext(req)

    result = _upload_rtunnel_via_contents_api(ctx, "https://nb.example.com/lab", binary)
    assert result is True
    assert len(req.calls) == 1

    url, _headers, data, timeout = req.calls[0]
    assert url == f"https://nb.example.com/api/contents/{_CONTENTS_API_RTUNNEL_FILENAME}"
    assert data["type"] == "file"
    assert data["format"] == "base64"
    # Verify the payload round-trips
    import base64 as _b64

    assert _b64.b64decode(data["content"]) == b"\x7fELF_test_binary"
    assert timeout == 30000


def test_upload_rtunnel_via_contents_api_failure_status(tmp_path: Path) -> None:
    binary = tmp_path / "rtunnel"
    binary.write_bytes(b"\x7fELF")

    resp = _DummyUploadResponse(500)
    req = _DummyUploadRequest(resp)
    ctx = _DummyUploadContext(req)

    result = _upload_rtunnel_via_contents_api(ctx, "https://nb.example.com/lab", binary)
    assert result is False


def test_upload_rtunnel_via_contents_api_missing_binary() -> None:
    resp = _DummyUploadResponse(201)
    req = _DummyUploadRequest(resp)
    ctx = _DummyUploadContext(req)

    result = _upload_rtunnel_via_contents_api(
        ctx, "https://nb.example.com/lab", Path("/nonexistent/rtunnel")
    )
    assert result is False
    assert len(req.calls) == 0


def test_upload_rtunnel_via_contents_api_network_error(tmp_path: Path) -> None:
    binary = tmp_path / "rtunnel"
    binary.write_bytes(b"\x7fELF")

    class _BrokenUploadRequest:
        def put(self, url: str, **kwargs: object) -> None:
            raise ConnectionError("network failure")

    ctx = _DummyUploadContext(_BrokenUploadRequest())  # type: ignore[arg-type]

    result = _upload_rtunnel_via_contents_api(ctx, "https://nb.example.com/lab", binary)
    assert result is False


# ---------------------------------------------------------------------------
# hash helpers + upload resolution
# ---------------------------------------------------------------------------


class _DummyContentsGetResponse:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status = status
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _DummyContentsRequest:
    def __init__(
        self,
        *,
        get_responses: list[_DummyContentsGetResponse] | None = None,
        put_status: int = 201,
    ) -> None:
        self._get_responses = list(get_responses or [])
        self.get_calls: list[tuple[str, int]] = []
        self.put_calls: list[tuple[str, dict | None, dict | None, int]] = []
        self.put_status = put_status

    def get(self, url: str, timeout: int = 0) -> _DummyContentsGetResponse:
        self.get_calls.append((url, timeout))
        if not self._get_responses:
            raise AssertionError("unexpected GET request")
        return self._get_responses.pop(0)

    def put(
        self,
        url: str,
        headers: dict | None = None,
        data: dict | None = None,
        timeout: int = 0,
    ) -> _DummyUploadResponse:
        self.put_calls.append((url, headers, data, timeout))
        return _DummyUploadResponse(self.put_status)


class _DummyContentsContext:
    def __init__(self, request: _DummyContentsRequest) -> None:
        self.request = request

    def cookies(self) -> list[dict]:
        return []


def test_compute_rtunnel_hash(tmp_path: Path) -> None:
    import hashlib

    binary = tmp_path / "rtunnel"
    binary.write_bytes(b"test-binary")

    assert _compute_rtunnel_hash(binary) == hashlib.sha256(b"test-binary").hexdigest()


def test_rtunnel_matches_on_notebook_success() -> None:
    import base64 as _b64

    hex_hash = "abc123"
    request = _DummyContentsRequest(
        get_responses=[
            _DummyContentsGetResponse(200, {"path": _CONTENTS_API_RTUNNEL_FILENAME}),
            _DummyContentsGetResponse(
                200,
                {"content": _b64.b64encode(hex_hash.encode("ascii")).decode("ascii")},
            ),
        ]
    )
    ctx = _DummyContentsContext(request)

    assert _rtunnel_matches_on_notebook(ctx, "https://nb.example.com/lab", hex_hash) is True
    assert request.get_calls[0][0].endswith(
        f"/api/contents/{_CONTENTS_API_RTUNNEL_FILENAME}?content=0"
    )
    assert request.get_calls[1][0].endswith(
        f"/api/contents/{_CONTENTS_API_RTUNNEL_FILENAME}.sha256?format=base64&content=1"
    )


def test_rtunnel_matches_on_notebook_missing_sidecar() -> None:
    request = _DummyContentsRequest(
        get_responses=[
            _DummyContentsGetResponse(200, {"path": _CONTENTS_API_RTUNNEL_FILENAME}),
            _DummyContentsGetResponse(404),
        ]
    )
    ctx = _DummyContentsContext(request)

    assert _rtunnel_matches_on_notebook(ctx, "https://nb.example.com/lab", "abc123") is False


def test_upload_rtunnel_hash_sidecar_success() -> None:
    request = _DummyContentsRequest(put_status=201)
    ctx = _DummyContentsContext(request)

    assert _upload_rtunnel_hash_sidecar(ctx, "https://nb.example.com/lab", "deadbeef") is True
    assert request.put_calls
    assert request.put_calls[0][0].endswith(
        f"/api/contents/{_CONTENTS_API_RTUNNEL_FILENAME}.sha256"
    )


def test_resolve_rtunnel_binary_policy_never(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _DummyContentsContext(_DummyContentsRequest())
    runtime = rtunnel_module.SshRuntimeConfig(
        rtunnel_bin="/shared/rtunnel",
        rtunnel_upload_policy="never",
    )

    monkeypatch.setattr(rtunnel_module.Path, "home", lambda: Path("/tmp/nonexistent-home"))

    assert (
        _resolve_rtunnel_binary(
            context=ctx,
            lab_url="https://nb.example.com/lab",
            ssh_runtime=runtime,
        )
        is None
    )


def test_resolve_rtunnel_binary_reuses_matching_notebook_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / ".local" / "bin" / "rtunnel"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"local-binary")
    ctx = _DummyContentsContext(_DummyContentsRequest())
    runtime = rtunnel_module.SshRuntimeConfig(
        rtunnel_bin="/shared/rtunnel",
        rtunnel_upload_policy="auto",
    )

    monkeypatch.setattr(rtunnel_module.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(rtunnel_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(rtunnel_module, "_rtunnel_matches_on_notebook", lambda *_a, **_k: True)
    monkeypatch.setattr(rtunnel_module, "_upload_rtunnel_via_contents_api", lambda *_a, **_k: False)

    assert (
        _resolve_rtunnel_binary(
            context=ctx,
            lab_url="https://nb.example.com/lab",
            ssh_runtime=runtime,
        )
        == _CONTENTS_API_RTUNNEL_FILENAME
    )


def test_resolve_rtunnel_binary_always_uploads_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / ".local" / "bin" / "rtunnel"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"local-binary")
    events: list[str] = []
    ctx = _DummyContentsContext(_DummyContentsRequest())
    runtime = rtunnel_module.SshRuntimeConfig(
        rtunnel_bin="/shared/rtunnel",
        rtunnel_upload_policy="always",
    )

    monkeypatch.setattr(rtunnel_module.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(rtunnel_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(rtunnel_module, "_rtunnel_matches_on_notebook", lambda *_a, **_k: False)
    monkeypatch.setattr(
        rtunnel_module,
        "_upload_rtunnel_via_contents_api",
        lambda *_a, **_k: events.append("upload") or True,
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_upload_rtunnel_hash_sidecar",
        lambda *_a, **_k: events.append("sidecar") or True,
    )

    assert (
        _resolve_rtunnel_binary(
            context=ctx,
            lab_url="https://nb.example.com/lab",
            ssh_runtime=runtime,
        )
        == _CONTENTS_API_RTUNNEL_FILENAME
    )
    assert events == ["upload", "sidecar"]


def test_resolve_rtunnel_binary_skips_host_incompatible_auto_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / ".local" / "bin" / "rtunnel"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"host-binary")
    ctx = _DummyContentsContext(_DummyContentsRequest())

    monkeypatch.setattr(rtunnel_module.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(rtunnel_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        rtunnel_module,
        "_rtunnel_matches_on_notebook",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not hash-check")),
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_upload_rtunnel_via_contents_api",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not upload")),
    )

    assert (
        _resolve_rtunnel_binary(
            context=ctx,
            lab_url="https://nb.example.com/lab",
            ssh_runtime=rtunnel_module.SshRuntimeConfig(rtunnel_upload_policy="auto"),
        )
        is None
    )


# ---------------------------------------------------------------------------
# _download_rtunnel_locally
# ---------------------------------------------------------------------------


def test_download_rtunnel_locally_success(tmp_path: Path) -> None:
    import tarfile

    # Build a valid .tar.gz containing a file named "rtunnel"
    binary_content = b"\x7fELF_fake_rtunnel"
    tar_path = tmp_path / "rtunnel.tar.gz"
    member_path = tmp_path / "rtunnel"
    member_path.write_bytes(binary_content)
    with tarfile.open(str(tar_path), "w:gz") as tar:
        tar.add(str(member_path), arcname="rtunnel")

    dest = tmp_path / "output" / "rtunnel"

    import shutil
    import urllib.request

    original_urlretrieve = urllib.request.urlretrieve

    def fake_urlretrieve(url: str, filename: str) -> tuple:
        shutil.copy2(str(tar_path), filename)
        return (filename, None)

    urllib.request.urlretrieve = fake_urlretrieve  # type: ignore[assignment]
    try:
        result = _download_rtunnel_locally("https://example.com/rtunnel.tar.gz", dest)
    finally:
        urllib.request.urlretrieve = original_urlretrieve  # type: ignore[assignment]

    assert result is True
    assert dest.exists()
    assert dest.read_bytes() == binary_content
    assert dest.stat().st_mode & 0o755


def test_download_rtunnel_locally_network_error(tmp_path: Path) -> None:
    import urllib.error
    import urllib.request

    dest = tmp_path / "rtunnel"

    original_urlretrieve = urllib.request.urlretrieve

    def broken_urlretrieve(url: str, filename: str) -> None:
        raise urllib.error.URLError("network failure")

    urllib.request.urlretrieve = broken_urlretrieve  # type: ignore[assignment]
    try:
        result = _download_rtunnel_locally("https://example.com/rtunnel.tar.gz", dest)
    finally:
        urllib.request.urlretrieve = original_urlretrieve  # type: ignore[assignment]

    assert result is False
    assert not dest.exists()


def test_download_rtunnel_locally_no_rtunnel_in_archive(tmp_path: Path) -> None:
    import tarfile

    # Build a .tar.gz with no file named "rtunnel"
    tar_path = tmp_path / "bad.tar.gz"
    other_file = tmp_path / "other.txt"
    other_file.write_text("not rtunnel")
    with tarfile.open(str(tar_path), "w:gz") as tar:
        tar.add(str(other_file), arcname="other.txt")

    dest = tmp_path / "output" / "rtunnel"

    import shutil
    import urllib.request

    original_urlretrieve = urllib.request.urlretrieve

    def fake_urlretrieve(url: str, filename: str) -> tuple:
        shutil.copy2(str(tar_path), filename)
        return (filename, None)

    urllib.request.urlretrieve = fake_urlretrieve  # type: ignore[assignment]
    try:
        result = _download_rtunnel_locally("https://example.com/rtunnel.tar.gz", dest)
    finally:
        urllib.request.urlretrieve = original_urlretrieve  # type: ignore[assignment]

    assert result is False
    assert not dest.exists()
