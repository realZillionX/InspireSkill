"""Tests for notebook rtunnel fast-path probe and state helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from inspire.bridge.tunnel.models import BridgeProfile, TunnelConfig
from inspire.platform.web.browser_api import rtunnel as rtunnel_module
from inspire.platform.web.session import WebSession


class DummyResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.closed = False

    def iter_content(self, chunk_size: int, decode_unicode: bool = False):  # noqa: ANN201
        del chunk_size
        if not self.text:
            return
        yield self.text if decode_unicode else self.text.encode()

    def close(self) -> None:
        self.closed = True


class DummyHTTP:
    def __init__(self, responses: dict[str, DummyResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.kwargs: list[dict] = []

    def get(self, url: str, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(url)
        self.kwargs.append(kwargs)
        return self.responses.get(url, DummyResponse(404, "not found"))

    def close(self) -> None:
        pass


def _session() -> WebSession:
    return WebSession(
        storage_state={"cookies": [{"name": "s", "value": "v"}], "origins": []},
        created_at=0,
        login_username="user-1",
    )


def test_state_cache_round_trip_and_ttl(tmp_path: Path) -> None:
    rtunnel_module.save_rtunnel_proxy_state(
        notebook_id="nb-1",
        proxy_url="https://nat.example/ws/x/notebook/nb-1/proxy/31337/",
        port=31337,
        ssh_port=22222,
        base_url="https://qz.example",
        account="user-1",
        cache_dir=tmp_path,
        now_ts=100.0,
    )

    urls = rtunnel_module.get_cached_rtunnel_proxy_candidates(
        notebook_id="nb-1",
        port=31337,
        base_url="https://qz.example",
        account="user-1",
        cache_dir=tmp_path,
        ttl_seconds=3600,
        now_ts=200.0,
    )
    assert urls == ["https://nat.example/ws/x/notebook/nb-1/proxy/31337/"]

    stale_urls = rtunnel_module.get_cached_rtunnel_proxy_candidates(
        notebook_id="nb-1",
        port=31337,
        base_url="https://qz.example",
        account="user-1",
        cache_dir=tmp_path,
        ttl_seconds=30,
        now_ts=200.0,
    )
    assert stale_urls == []


def test_probe_uses_cached_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session()
    base_url = "https://qz.example"
    notebook_id = "notebook-123"
    known_url = f"{base_url}/api/v1/notebook/lab/{notebook_id}/proxy/31337/"
    cached_url = "https://nat.example/ws/x/vscode/notebook-123/token/proxy/31337/?token=t"

    monkeypatch.setattr(rtunnel_module, "_get_base_url", lambda: base_url)
    monkeypatch.setattr(
        rtunnel_module,
        "get_cached_rtunnel_proxy_candidates",
        lambda **_kwargs: [cached_url],
    )
    monkeypatch.setattr(
        rtunnel_module,
        "save_rtunnel_proxy_state",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        rtunnel_module,
        "load_tunnel_config",
        lambda account=None: TunnelConfig(account=account),
    )

    http = DummyHTTP(
        {
            known_url: DummyResponse(404, "not found"),
            cached_url: DummyResponse(200, ""),
        }
    )
    monkeypatch.setattr(rtunnel_module, "build_requests_session", lambda _session, _base: http)

    resolved = rtunnel_module.probe_existing_rtunnel_proxy_url(
        notebook_id=notebook_id,
        port=31337,
        session=session,
        account="user-1",
    )

    assert resolved == cached_url
    assert known_url in http.calls
    assert cached_url in http.calls
    assert all(call["stream"] is True for call in http.kwargs)
    assert all(call["timeout"] == (5, 5) for call in http.kwargs)


def test_probe_uses_ide_port_forward_candidate_with_ssh_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    base_url = "https://qz.example"
    notebook_id = "notebook-ide"
    ide_proxy_url = "https://nat.example/ws/x/vscode/notebook-ide/token/proxy/31337/"
    saved: list[dict[str, object]] = []
    probed: list[dict[str, object]] = []

    monkeypatch.setattr(rtunnel_module, "_get_base_url", lambda: base_url)
    monkeypatch.setattr(
        rtunnel_module,
        "_candidate_urls_from_ide_port_forward",
        lambda **_kwargs: [ide_proxy_url],
    )
    monkeypatch.setattr(
        rtunnel_module,
        "get_cached_rtunnel_proxy_candidates",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_candidate_urls_from_tunnel_config",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        rtunnel_module,
        "_ssh_probe_rtunnel_proxy_url",
        lambda **kwargs: probed.append(kwargs) or kwargs["proxy_url"] == ide_proxy_url,
    )
    monkeypatch.setattr(
        rtunnel_module, "save_rtunnel_proxy_state", lambda **kwargs: saved.append(kwargs)
    )
    monkeypatch.setattr(
        rtunnel_module,
        "build_requests_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("HTTP probe must not run")),
    )

    resolved = rtunnel_module.probe_existing_rtunnel_proxy_url(
        notebook_id=notebook_id,
        port=31337,
        ssh_port=22222,
        session=session,
        account="user-1",
        verify_ssh=True,
        include_ide_port_forward=True,
        ssh_public_key="ssh-ed25519 AAA test-key",
    )

    assert resolved == ide_proxy_url
    assert probed[0]["ssh_public_key"] == "ssh-ed25519 AAA test-key"
    assert saved[0]["proxy_url"] == ide_proxy_url
    assert saved[0]["ssh_port"] == 22222


def test_probe_uses_tunnel_profile_and_rewrites_proxy_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    base_url = "https://qz.example"
    notebook_id = "notebook-abc"
    known_url = f"{base_url}/api/v1/notebook/lab/{notebook_id}/proxy/31337/"
    bridge_url = "https://nat.example/ws/demo/user/vscode/notebook-abc/aaa/proxy/22222/?token=abc"
    rewritten = "https://nat.example/ws/demo/user/vscode/notebook-abc/aaa/proxy/31337/?token=abc"

    monkeypatch.setattr(rtunnel_module, "_get_base_url", lambda: base_url)
    monkeypatch.setattr(
        rtunnel_module,
        "get_cached_rtunnel_proxy_candidates",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        rtunnel_module,
        "save_rtunnel_proxy_state",
        lambda **_kwargs: None,
    )

    config = TunnelConfig(account="user-1")
    config.add_bridge(BridgeProfile(name="bridge-1", proxy_url=bridge_url))
    monkeypatch.setattr(rtunnel_module, "load_tunnel_config", lambda account=None: config)

    http = DummyHTTP(
        {
            known_url: DummyResponse(404, "not found"),
            rewritten: DummyResponse(200, "rtunnel ready"),
        }
    )
    monkeypatch.setattr(rtunnel_module, "build_requests_session", lambda _session, _base: http)

    resolved = rtunnel_module.probe_existing_rtunnel_proxy_url(
        notebook_id=notebook_id,
        port=31337,
        session=session,
        account="user-1",
    )

    assert resolved == rewritten


def test_probe_rejects_html_response(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session()
    base_url = "https://qz.example"
    notebook_id = "nb-html"
    known_url = f"{base_url}/api/v1/notebook/lab/{notebook_id}/proxy/31337/"

    monkeypatch.setattr(rtunnel_module, "_get_base_url", lambda: base_url)
    monkeypatch.setattr(
        rtunnel_module,
        "get_cached_rtunnel_proxy_candidates",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        rtunnel_module,
        "save_rtunnel_proxy_state",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        rtunnel_module,
        "load_tunnel_config",
        lambda account=None: TunnelConfig(account=account),
    )

    http = DummyHTTP({known_url: DummyResponse(200, "<html>not notebook proxy</html>")})
    monkeypatch.setattr(rtunnel_module, "build_requests_session", lambda _session, _base: http)

    resolved = rtunnel_module.probe_existing_rtunnel_proxy_url(
        notebook_id=notebook_id,
        port=31337,
        session=session,
        account="user-1",
    )
    assert resolved is None


def test_probe_reads_only_stream_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session()
    base_url = "https://qz.example"
    notebook_id = "nb-stream"
    known_url = f"{base_url}/api/v1/notebook/lab/{notebook_id}/proxy/31337/"

    class StreamingResponse:
        status_code = 200
        closed = False

        @property
        def text(self) -> str:
            raise AssertionError("probe must not read the full response body")

        def iter_content(self, chunk_size: int, decode_unicode: bool = False):  # noqa: ANN201
            del chunk_size
            yield "rtunnel ready" if decode_unicode else b"rtunnel ready"

        def close(self) -> None:
            self.closed = True

    response = StreamingResponse()

    monkeypatch.setattr(rtunnel_module, "_get_base_url", lambda: base_url)
    monkeypatch.setattr(
        rtunnel_module,
        "get_cached_rtunnel_proxy_candidates",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        rtunnel_module,
        "save_rtunnel_proxy_state",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        rtunnel_module,
        "load_tunnel_config",
        lambda account=None: TunnelConfig(account=account),
    )

    http = DummyHTTP({known_url: response})
    monkeypatch.setattr(rtunnel_module, "build_requests_session", lambda _session, _base: http)

    resolved = rtunnel_module.probe_existing_rtunnel_proxy_url(
        notebook_id=notebook_id,
        port=31337,
        session=session,
        account="user-1",
    )

    assert resolved == known_url
    assert response.closed is True
