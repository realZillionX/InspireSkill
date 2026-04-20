"""Tests for notebook rtunnel proxy verification flow helpers."""

from __future__ import annotations

import pytest

from inspire.platform.web.browser_api import rtunnel as flow_module


class DummyLocator:
    def __init__(self, count: int = 0) -> None:
        self._count = count
        self.first = self

    def count(self) -> int:
        return self._count

    def click(self, timeout: int = 0) -> None:
        assert timeout >= 0


class DummyPage:
    def locator(self, selector: str) -> DummyLocator:
        assert selector
        return DummyLocator(count=0)

    def wait_for_timeout(self, timeout_ms: int) -> None:
        assert timeout_ms >= 0


@pytest.mark.parametrize("timeout", [30, 120])
def test_ensure_proxy_readiness_prefers_vscode_when_available(
    monkeypatch: pytest.MonkeyPatch,
    timeout: int,
) -> None:
    primary_url = "https://nat.example/jupyter/nb/proxy/31337/"
    derived_url = "https://nat.example/vscode/nb/proxy/31337/"
    calls: list[str] = []

    def fake_wait_for_rtunnel_reachable(*, proxy_url, timeout_s, context, page) -> None:  # type: ignore[no-untyped-def]
        assert timeout_s > 0
        assert context is not None
        assert page is not None
        calls.append(proxy_url)
        if proxy_url == primary_url:
            raise AssertionError("primary probe should not be attempted when vscode passes")

    monkeypatch.setattr(flow_module, "wait_for_rtunnel_reachable", fake_wait_for_rtunnel_reachable)

    resolved, diagnostics = flow_module._ensure_proxy_readiness_with_fallback(
        proxy_url=primary_url,
        port=31337,
        timeout=timeout,
        context=object(),
        page=DummyPage(),
    )

    assert resolved == derived_url
    assert calls == [derived_url]
    assert diagnostics == []


def test_ensure_proxy_readiness_falls_back_to_primary_when_vscode_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_url = "https://nat.example/jupyter/nb/proxy/31337/"
    derived_url = "https://nat.example/vscode/nb/proxy/31337/"
    calls: list[str] = []

    def fake_wait_for_rtunnel_reachable(*, proxy_url, timeout_s, context, page) -> None:  # type: ignore[no-untyped-def]
        assert timeout_s > 0
        assert context is not None
        assert page is not None
        calls.append(proxy_url)
        if proxy_url == derived_url:
            raise ValueError("vscode failed\nLast response: 404 page not found")

    monkeypatch.setattr(flow_module, "wait_for_rtunnel_reachable", fake_wait_for_rtunnel_reachable)

    resolved, diagnostics = flow_module._ensure_proxy_readiness_with_fallback(
        proxy_url=primary_url,
        port=31337,
        timeout=60,
        context=object(),
        page=DummyPage(),
    )

    assert resolved == primary_url
    assert calls == [derived_url, primary_url]
    assert len(diagnostics) == 1
    assert diagnostics[0].startswith("derived=")
    assert "Last response: 404 page not found" in diagnostics[0]


def test_ensure_proxy_readiness_does_not_raise_when_both_probes_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_url = "https://nat.example/jupyter/nb/proxy/31337/"
    derived_url = "https://nat.example/vscode/nb/proxy/31337/"
    fallback_url = "https://nat.example/vscode/nb/proxy/31337/?token=abc"
    calls: list[str] = []

    def fake_wait_for_rtunnel_reachable(*, proxy_url, timeout_s, context, page) -> None:  # type: ignore[no-untyped-def]
        assert timeout_s > 0
        assert context is not None
        assert page is not None
        calls.append(proxy_url)
        raise ValueError(f"probe failed for {proxy_url}\nLast response: 404 page not found")

    monkeypatch.setattr(flow_module, "wait_for_rtunnel_reachable", fake_wait_for_rtunnel_reachable)
    monkeypatch.setattr(flow_module, "_build_vscode_proxy_url", lambda _page, port: fallback_url)

    resolved, diagnostics = flow_module._ensure_proxy_readiness_with_fallback(
        proxy_url=primary_url,
        port=31337,
        timeout=60,
        context=object(),
        page=DummyPage(),
    )

    assert resolved == fallback_url
    assert calls == [derived_url, primary_url, fallback_url]
    assert len(diagnostics) == 3
    assert diagnostics[0].startswith("derived=")
    assert diagnostics[1].startswith("primary=")
    assert diagnostics[2].startswith("fallback=")


def test_ensure_proxy_readiness_without_fallback_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_url = "https://nat.example/jupyter/nb/proxy/31337/"
    derived_url = "https://nat.example/vscode/nb/proxy/31337/"
    calls: list[str] = []

    def fake_wait_for_rtunnel_reachable(*, proxy_url, timeout_s, context, page) -> None:  # type: ignore[no-untyped-def]
        assert timeout_s > 0
        assert context is not None
        assert page is not None
        calls.append(proxy_url)
        raise ValueError(f"probe failed for {proxy_url}\nLast response: 404 page not found")

    monkeypatch.setattr(flow_module, "wait_for_rtunnel_reachable", fake_wait_for_rtunnel_reachable)
    monkeypatch.setattr(flow_module, "_build_vscode_proxy_url", lambda _page, port: None)

    resolved, diagnostics = flow_module._ensure_proxy_readiness_with_fallback(
        proxy_url=primary_url,
        port=31337,
        timeout=60,
        context=object(),
        page=DummyPage(),
    )

    # When all probes fail and no page-built fallback is found, the primary
    # (jupyter) URL should be returned as the best guess -- not the
    # speculative derived vscode URL.
    assert resolved == primary_url
    assert calls == [derived_url, primary_url]
    assert len(diagnostics) == 2
    assert diagnostics[0].startswith("derived=")
    assert diagnostics[1].startswith("primary=")


def test_ensure_proxy_readiness_reports_inconclusive_http_probe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    primary_url = "https://nat.example/jupyter/nb/proxy/31337/"

    def fake_wait_for_rtunnel_reachable(*, proxy_url, timeout_s, context, page) -> None:  # type: ignore[no-untyped-def]
        assert proxy_url
        assert timeout_s > 0
        assert context is not None
        assert page is not None
        raise ValueError(
            "HTTP readiness probe stayed inconclusive with plain-text 404 on 3 consecutive "
            "attempts (2s elapsed).\nLast response: 404 page not found"
        )

    monkeypatch.setattr(flow_module, "wait_for_rtunnel_reachable", fake_wait_for_rtunnel_reachable)
    monkeypatch.setattr(flow_module, "_build_vscode_proxy_url", lambda _page, port: None)

    resolved, diagnostics = flow_module._ensure_proxy_readiness_with_fallback(
        proxy_url=primary_url,
        port=31337,
        timeout=60,
        context=object(),
        page=DummyPage(),
    )

    captured = capsys.readouterr()
    assert resolved == primary_url
    assert len(diagnostics) == 2
    assert "HTTP readiness probe was inconclusive" in captured.err
    assert "Proxy did not pass HTTP readiness" not in captured.err
