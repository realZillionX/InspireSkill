from __future__ import annotations

import importlib

import pytest

from inspire.config import Config
from inspire.platform.openapi import InspireAPI, InspireConfig


def test_inspire_api_honors_verify_ssl_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INSPIRE_SKIP_SSL_VERIFY", raising=False)

    api = InspireAPI(InspireConfig(verify_ssl=False))

    assert api.config.verify_ssl is False


def test_inspire_api_env_override_disables_verify_ssl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSPIRE_SKIP_SSL_VERIFY", "1")

    api = InspireAPI(InspireConfig(verify_ssl=True))

    assert api.config.verify_ssl is False


def test_inspire_api_force_proxy_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INSPIRE_FORCE_PROXY", raising=False)
    monkeypatch.setenv("http_proxy", "http://proxy.example.com:8080")
    monkeypatch.setenv("https_proxy", "http://proxy-secure.example.com:8443")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.com:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-secure.example.com:8443")

    api = InspireAPI(InspireConfig(force_proxy=True))

    assert api.session.proxies == {
        "http": "http://proxy.example.com:8080",
        "https": "http://proxy-secure.example.com:8443",
    }


def test_inspire_api_force_proxy_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSPIRE_FORCE_PROXY", "true")
    monkeypatch.setenv("http_proxy", "http://proxy.example.com:8080")
    monkeypatch.setenv("https_proxy", "http://proxy-secure.example.com:8443")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.com:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-secure.example.com:8443")

    api = InspireAPI(InspireConfig(force_proxy=False))

    assert api.session.proxies == {
        "http": "http://proxy.example.com:8080",
        "https": "http://proxy-secure.example.com:8443",
    }


def test_auth_manager_passes_ssl_and_proxy_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    auth_module = importlib.import_module("inspire.cli.utils.auth")

    captured: dict[str, InspireConfig] = {}

    class FakeAPI:
        def __init__(self, config: InspireConfig) -> None:
            captured["config"] = config
            self.token = None

        def authenticate(self, username: str, password: str) -> bool:  # noqa: ARG002
            self.token = "token-123"
            return True

    monkeypatch.setattr(auth_module, "InspireAPI", FakeAPI)
    auth_module.AuthManager.clear_cache()

    config = Config(
        username="user",
        password="pass",
        skip_ssl_verify=True,
        force_proxy=True,
    )
    api = auth_module.AuthManager.get_api(config)

    assert api.token == "token-123"
    assert captured["config"].verify_ssl is False
    assert captured["config"].force_proxy is True

    auth_module.AuthManager.clear_cache()
