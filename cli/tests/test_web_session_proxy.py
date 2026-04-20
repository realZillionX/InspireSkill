from __future__ import annotations

import pytest

from inspire import config as config_module
from inspire.platform.web.session.proxy import (
    get_playwright_proxy,
    get_rtunnel_proxy_override,
    resolve_requests_proxy_config,
)


@pytest.fixture(autouse=True)
def clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [
        "INSPIRE_PLAYWRIGHT_PROXY",
        "inspire_playwright_proxy",
        "PLAYWRIGHT_PROXY",
        "INSPIRE_RTUNNEL_PROXY",
        "inspire_rtunnel_proxy",
        "INSPIRE_REQUESTS_HTTP_PROXY",
        "INSPIRE_REQUESTS_HTTPS_PROXY",
        "INSPIRE_BASE_URL",
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
    ]:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **kwargs: (_ for _ in ()).throw(RuntimeError("no config"))),
    )


def test_get_playwright_proxy_prefers_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSPIRE_PLAYWRIGHT_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")

    assert get_playwright_proxy() == {"server": "http://127.0.0.1:7897"}


def test_get_playwright_proxy_reuses_qizhi_mixed_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INSPIRE_BASE_URL", "https://qz.sii.edu.cn")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7897")

    assert get_playwright_proxy() == {"server": "http://127.0.0.1:7897"}


def test_get_playwright_proxy_falls_back_to_http_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSPIRE_BASE_URL", "https://example.com")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")

    assert get_playwright_proxy() == {"server": "http://127.0.0.1:7897"}


def test_get_playwright_proxy_uses_proxy_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config_module.Config(
        username="",
        password="",
        base_url="https://qz.sii.edu.cn",
        playwright_proxy="http://127.0.0.1:7897",
    )
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **kwargs: (cfg, {})),
    )

    assert get_playwright_proxy() == {"server": "http://127.0.0.1:7897"}


def test_resolve_requests_proxy_config_prefers_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config_module.Config(
        username="",
        password="",
        base_url="https://qz.sii.edu.cn",
        requests_http_proxy="http://127.0.0.1:7897",
        requests_https_proxy="http://127.0.0.1:7897",
    )
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **kwargs: (cfg, {})),
    )

    proxies, source = resolve_requests_proxy_config()
    assert source == "toml"
    assert proxies == {
        "http": "http://127.0.0.1:7897",
        "https": "http://127.0.0.1:7897",
    }


def test_get_rtunnel_proxy_override_uses_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config_module.Config(
        username="",
        password="",
        base_url="https://qz.sii.edu.cn",
        rtunnel_proxy="http://127.0.0.1:7897",
    )
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **kwargs: (cfg, {})),
    )

    assert get_rtunnel_proxy_override() == "http://127.0.0.1:7897"
