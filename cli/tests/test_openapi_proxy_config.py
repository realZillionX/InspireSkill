from __future__ import annotations

import pytest

from inspire.platform.openapi.client import InspireAPI
from inspire.platform.openapi.models import InspireConfig


class _DummySession:
    def __init__(self) -> None:
        self.proxies: dict[str, str] = {}
        self.trust_env = True


@pytest.fixture(autouse=True)
def clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [
        "INSPIRE_FORCE_PROXY",
        "INSPIRE_REQUESTS_HTTP_PROXY",
        "INSPIRE_REQUESTS_HTTPS_PROXY",
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
    ]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def patch_requests_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "inspire.platform.openapi.client.requests.Session",
        lambda: _DummySession(),
    )


def test_openapi_proxy_prefers_explicit_env_over_config(
    monkeypatch: pytest.MonkeyPatch,
    patch_requests_session: None,
) -> None:
    monkeypatch.setenv("INSPIRE_REQUESTS_HTTP_PROXY", "http://127.0.0.1:19999")
    monkeypatch.setenv("INSPIRE_REQUESTS_HTTPS_PROXY", "http://127.0.0.1:19999")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:18888")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:18888")

    cfg = InspireConfig(
        base_url="https://qz.sii.edu.cn",
        requests_http_proxy="http://127.0.0.1:17777",
        requests_https_proxy="http://127.0.0.1:17777",
    )
    api = InspireAPI(cfg)

    assert api.session.proxies == {
        "http": "http://127.0.0.1:19999",
        "https": "http://127.0.0.1:19999",
    }
    assert api.session.trust_env is True


def test_openapi_proxy_uses_config_when_explicit_env_missing(
    patch_requests_session: None,
) -> None:
    cfg = InspireConfig(
        base_url="https://qz.sii.edu.cn",
        requests_http_proxy="http://127.0.0.1:7897",
        requests_https_proxy="http://127.0.0.1:7897",
    )
    api = InspireAPI(cfg)

    assert api.session.proxies == {
        "http": "http://127.0.0.1:7897",
        "https": "http://127.0.0.1:7897",
    }


def test_openapi_proxy_falls_back_to_system_env(
    monkeypatch: pytest.MonkeyPatch,
    patch_requests_session: None,
) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7897")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")

    api = InspireAPI(InspireConfig(base_url="https://qz.sii.edu.cn"))

    assert api.session.proxies == {
        "http": "http://127.0.0.1:7897",
        "https": "http://127.0.0.1:7897",
    }


def test_openapi_force_proxy_from_config_disables_trust_env(
    patch_requests_session: None,
) -> None:
    cfg = InspireConfig(
        base_url="https://qz.sii.edu.cn",
        force_proxy=True,
        requests_http_proxy="http://127.0.0.1:7897",
        requests_https_proxy="http://127.0.0.1:7897",
    )
    api = InspireAPI(cfg)

    assert api.session.trust_env is False
    assert api.session.proxies["http"] == "http://127.0.0.1:7897"


def test_openapi_force_proxy_env_overrides_config(
    monkeypatch: pytest.MonkeyPatch,
    patch_requests_session: None,
) -> None:
    monkeypatch.setenv("INSPIRE_FORCE_PROXY", "true")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7897")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")

    cfg = InspireConfig(base_url="https://qz.sii.edu.cn", force_proxy=False)
    api = InspireAPI(cfg)

    assert api.session.trust_env is False
    assert api.session.proxies["http"] == "http://127.0.0.1:7897"
