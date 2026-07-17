from __future__ import annotations

import pytest

from inspire import config as config_module
from inspire.platform.web.session.proxy import (
    describe_effective_proxy_config,
    get_playwright_proxy,
    get_rtunnel_proxy_override,
    redact_proxy_url,
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
        "NO_PROXY",
        "no_proxy",
        "ALL_PROXY",
        "all_proxy",
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


def test_get_playwright_proxy_applies_no_proxy_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")
    monkeypatch.setenv(
        "NO_PROXY",
        "localhost,127.0.0.1,qz.sii.edu.cn,.sii.edu.cn,*.sii.edu.cn,qz.sii.edu.cn",
    )

    assert get_playwright_proxy() == {
        "server": "http://127.0.0.1:7897",
        "bypass": "localhost,127.0.0.1,qz.sii.edu.cn,*.sii.edu.cn",
    }


def test_lowercase_no_proxy_takes_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:18443")
    monkeypatch.setenv("NO_PROXY", ".sii.edu.cn")
    monkeypatch.setenv("no_proxy", ".example.org")

    assert get_playwright_proxy() == {
        "server": "http://proxy.example:18443",
        "bypass": "*.example.org",
    }
    summary = describe_effective_proxy_config(base_url="https://qz.sii.edu.cn")
    assert summary["requests"]["no_proxy"] == "not_matched"
    assert summary["requests"]["route"] == "proxy"


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


def test_get_playwright_proxy_uses_explicit_account_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str | None] = []

    def fake_from_files_and_env(cls, **kwargs):  # type: ignore[no-untyped-def]
        del cls
        account = kwargs.get("account")
        calls.append(account)
        proxy = "http://127.0.0.1:18080" if account == "bob" else "http://127.0.0.1:7897"
        return config_module.Config(
            username="",
            password="",
            base_url="https://qz.sii.edu.cn",
            playwright_proxy=proxy,
        ), {}

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(fake_from_files_and_env),
    )

    assert get_playwright_proxy(account="bob") == {"server": "http://127.0.0.1:18080"}
    assert calls == ["bob"]


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


def test_describe_effective_proxy_config_reports_shell_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:18080")
    monkeypatch.setenv("HTTPS_PROXY", "http://secure-proxy.example:18443")
    monkeypatch.setenv("NO_PROXY", ".sii.edu.cn")

    summary = describe_effective_proxy_config(base_url="https://qz.sii.edu.cn")

    assert summary == {
        "target": "qz.sii.edu.cn",
        "requests": {
            "source": "system_env",
            "route": "direct",
            "http": "http://proxy.example:18080",
            "https": "http://secure-proxy.example:18443",
            "all": None,
            "selected": "http://secure-proxy.example:18443",
            "no_proxy": "matched",
        },
        "playwright": {
            "source": "requests:system_env",
            "route": "direct",
            "server": "http://secure-proxy.example:18443",
            "no_proxy": "matched",
        },
        "rtunnel": {
            "source": "requests:system_env",
            "route": "proxy",
            "server": "http://secure-proxy.example:18443",
        },
    }


def test_system_proxy_preserves_scheme_specific_requests_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:18080")

    proxies, source = resolve_requests_proxy_config()
    summary = describe_effective_proxy_config(base_url="https://qz.sii.edu.cn")

    assert source == "system_env"
    assert proxies == {"http": "http://proxy.example:18080"}
    assert summary["requests"] == {
        "source": "system_env",
        "route": "direct",
        "http": "http://proxy.example:18080",
        "https": None,
        "all": None,
        "selected": None,
        "no_proxy": "not_set",
    }
    assert summary["playwright"] == {
        "source": "requests:system_env",
        "route": "proxy",
        "server": "http://proxy.example:18080",
        "no_proxy": "not_set",
    }


def test_describe_effective_proxy_config_none_is_stable() -> None:
    summary = describe_effective_proxy_config(base_url="https://qz.sii.edu.cn")

    assert summary["requests"] == {
        "source": "none",
        "route": "direct",
        "http": None,
        "https": None,
        "all": None,
        "selected": None,
        "no_proxy": "not_applicable",
    }
    assert summary["playwright"] == {
        "source": "none",
        "route": "direct",
        "server": None,
        "no_proxy": "not_applicable",
    }
    assert summary["rtunnel"] == {
        "source": "none",
        "route": "direct",
        "server": None,
    }


def test_effective_proxy_summary_redacts_credentials_and_url_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = "http://alice:secret@proxy.example:18443/private?token=value#fragment"
    monkeypatch.setenv("HTTPS_PROXY", proxy)

    summary = describe_effective_proxy_config(base_url="https://qz.sii.edu.cn")
    rendered = repr(summary)

    assert "alice" not in rendered
    assert "secret" not in rendered
    assert "private" not in rendered
    assert "token" not in rendered
    assert "value" not in rendered
    assert "http://<redacted>@proxy.example:18443" in rendered
    assert redact_proxy_url("proxy-without-a-scheme:7897") == "<configured>"


def test_all_proxy_is_modeled_for_every_fallback_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ALL_PROXY",
        "socks5://proxy-user:proxy-password@all-proxy.example:1080/private?token=value",
    )

    proxies, source = resolve_requests_proxy_config()
    summary = describe_effective_proxy_config(base_url="https://qz.sii.edu.cn")
    rendered = repr(summary)

    assert source == "system_env"
    assert proxies == {
        "all": (
            "socks5://proxy-user:proxy-password@all-proxy.example:1080/private?token=value"
        )
    }
    assert summary["requests"]["route"] == "proxy"
    assert summary["requests"]["selected"] == (
        "socks5://<redacted>@all-proxy.example:1080"
    )
    assert summary["playwright"]["source"] == "requests:system_env"
    assert summary["playwright"]["server"] == (
        "socks5://<redacted>@all-proxy.example:1080"
    )
    assert summary["rtunnel"]["server"] == "socks5://<redacted>@all-proxy.example:1080"
    assert "proxy-user" not in rendered
    assert "proxy-password" not in rendered
    assert "private" not in rendered
    assert "token" not in rendered
