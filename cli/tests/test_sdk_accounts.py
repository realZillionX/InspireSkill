"""Offline account and initialization contracts, sharing the SDK HTTP blocker."""

from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from inspire import Accounts, InspireClient, InitResult, AccountInfo, ValidationError
from inspire.accounts import account_scope
from inspire.config import Config
from inspire.platform.web.session import auth, DEFAULT_WORKSPACE_ID
from inspire.platform.web.session.models import WebSession
from inspire.services.account.account_config import atomic_write_text
from test_sdk import client as client


@pytest.fixture(autouse=True)
def offline(client, monkeypatch):
    monkeypatch.setattr("inspire.accounts.normalize._playwright_chromium_installed", lambda: True)

    def forbidden(*args, **kwargs):
        pytest.fail("No browser or interactive setup is allowed")

    monkeypatch.setattr(auth, "device_login", forbidden)
    monkeypatch.setattr("inspire.accounts.normalize._playwright_chromium_available", forbidden)
    monkeypatch.setattr("inspire.accounts.normalize._install_playwright_chromium", forbidden)


def test_accounts_roundtrip(client, capsys):
    Accounts.remove("alpha")
    Accounts.remove("beta")
    assert Accounts.list() == ()
    assert Accounts.current() is None
    assert Accounts.add("one") == "one"
    assert Accounts.current() == "one"
    Accounts.add("two", use=False)
    with account_scope("two"):
        assert Accounts.current() == "one"
    Accounts.use("two")
    Accounts.rename("two", "renamed")
    assert Accounts.current() == "renamed"
    assert Accounts.exists("renamed") and not Accounts.exists("two")
    assert Accounts.list() == ("one", "renamed")
    Accounts.remove("renamed")
    assert Accounts.current() is None
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("proxy", [None, "http://localhost:7897"])
def test_add_matches_cli(client, proxy):
    from inspire.cli.commands.account import account

    Accounts.add("sdk", proxy=proxy)
    args = ["add", "cli", "--username", "login", "--password", 'fake"password', "--non-interactive", "--no-login"]
    if proxy:
        args += ["--proxy", proxy]
    result = CliRunner().invoke(account, args)
    assert result.exit_code == 0, result.output
    assert "password" not in Accounts.config_path("cli").read_text()
    assert Config._load_toml(Accounts.config_path("sdk"))["api"] == Config._load_toml(Accounts.config_path("cli"))["api"]


def test_overwrite_and_errors(client):
    with pytest.raises(ValidationError, match="already exists: alpha"):
        Accounts.add("alpha")
    Accounts.add("alpha", overwrite=True)
    assert "auth" not in Config._load_toml(Accounts.config_path("alpha"))
    for call in [
        lambda: Accounts.use("missing"),
        lambda: Accounts.remove("missing"),
        lambda: Accounts.rename("missing", "new"),
        lambda: Accounts.config_path("../bad"),
    ]:
        with pytest.raises(ValidationError):
            call()


def test_deprecated_credentials_are_ignored(client):
    path = Accounts.config_path("beta")
    before = path.read_bytes()
    with pytest.warns(DeprecationWarning, match="ignored"):
        with InspireClient("beta", username="ignored") as created:
            assert created.account == "beta"
    with pytest.warns(DeprecationWarning, match="ignored"):
        with InspireClient.from_credentials("ignored", "ignored", account="beta"):
            pass
    assert path.read_bytes() == before
    assert Accounts.current() == "alpha"
    with pytest.warns(DeprecationWarning, match="ignored"):
        Accounts.add("new", password="ignored")
    assert "auth" not in Config._load_toml(Accounts.config_path("new"))


def test_login_cached(client, monkeypatch):
    refresh = Mock(side_effect=AssertionError("cached session must be reused"))
    monkeypatch.setattr(client._transport, "_refresh", refresh)
    user = Mock(return_value={"id": "u", "name": "User"})
    monkeypatch.setattr("inspire.platform.web.browser_api.get_current_user", user)
    result = client.login()
    assert isinstance(result, AccountInfo) and result.user_id == "u"
    assert result.username == client._transport.session.login_username
    assert user.call_args.kwargs["refresh"] is True
    refresh.assert_not_called()


@pytest.mark.parametrize("force", [False, True])
def test_login_refreshes_expired_tokens(client, monkeypatch, force):
    old = client._transport.session
    renewed = replace(old, access_expires_at=9999999999)
    old.access_expires_at = 0
    refresh = Mock(return_value=renewed)
    monkeypatch.setattr(auth, "refresh_session", refresh)
    monkeypatch.setattr("inspire.platform.web.browser_api.get_current_user", lambda **kw: {})
    assert isinstance(client.login(force=force), AccountInfo)
    refresh.assert_called_once_with(old, lock_timeout=refresh.call_args.kwargs["lock_timeout"])
    # deadline - now can round to 120.00000000000003 when a coarse clock (Windows)
    # returns the same reading for both samples; allow float slack, not real time.
    assert 0 < refresh.call_args.kwargs["lock_timeout"] <= 120 + 1e-6


def test_login_no_cache(client, monkeypatch):
    from inspire.sdk import AuthenticationError
    client._transport._session = None
    monkeypatch.setattr(WebSession, "load", lambda **kw: None)
    with pytest.raises(AuthenticationError, match="inspire login"):
        client.login()


def test_init_preserves_legacy_and_unknown_values(client, monkeypatch):
    monkeypatch.setattr("inspire.platform.web.browser_api.get_current_user", lambda **kw: {})
    monkeypatch.setattr(client._transport, "_refresh", lambda: None)
    path = Accounts.config_path(client.account)
    path.write_text(
        path.read_text()
        + """
extra_auth = "keep"
[api]
docker_registry = "x"
[custom]
keep = "yes"
date = 2026-09-08
timestamp = 2026-09-08T10:20:30Z
time = 10:20:30
control = "\\u0001"
mixed = [1, { nested = { keep = true } }]
[custom.empty]
[path_aliases]
data = "/legacy/data"
[project_catalog]
ids = ["old", "older"]
[projects]
retired = true
[[compute_groups]]
name = "legacy"
[compute_groups.resources]
gpu = 8
[[compute_groups.queues]]
name = "nested"
[[compute_groups]]
name = "other"
"""
    )
    expected = deepcopy(Config._load_toml(path))
    expected["auth"].pop("username")
    expected["auth"].pop("password")
    expected.setdefault("api", {})["base_url"] = client.base_url
    result = client.init()
    assert isinstance(result, InitResult) and result.changed and result.warnings == ()
    assert Config._load_toml(path) == expected
    before = path.read_bytes()
    write = Mock(wraps=atomic_write_text)
    monkeypatch.setattr("inspire.services.account.account_config.atomic_write_text", write)
    assert not client.init().changed
    write.assert_not_called()
    assert path.read_bytes() == before

    assert client.init(force=True).changed
    data = Config._load_toml(path)
    assert (
        not {"custom", "path_aliases", "project_catalog", "projects", "compute_groups"}
        & data.keys()
    )
    assert "docker_registry" not in data["api"]
    assert "auth" not in data
    assert data["api"]["base_url"] == client.base_url


def test_init_unchanged_dict_does_not_reformat(client, monkeypatch):
    monkeypatch.setattr("inspire.platform.web.browser_api.get_current_user", lambda **kw: {})
    path = Accounts.config_path(client.account)
    initial = f'# Keep formatting\n[api]\nbase_url="{client.base_url}"\n'
    path.write_text(initial)
    write = Mock(side_effect=AssertionError("Unchanged dictionaries must not be written"))
    monkeypatch.setattr("inspire.services.account.account_config.atomic_write_text", write)
    assert not client.init().changed
    write.assert_not_called()
    assert path.read_text() == initial


@pytest.mark.parametrize("workspace", [None, "", DEFAULT_WORKSPACE_ID])
def test_init_requires_workspace(client, monkeypatch, workspace):
    monkeypatch.setattr("inspire.platform.web.browser_api.get_current_user", lambda **kw: {})
    client._transport._session.workspace_id = workspace
    before = Accounts.config_path(client.account).read_bytes()
    with pytest.raises(ValidationError, match="Could not detect an accessible workspace"):
        client.init()
    assert Accounts.config_path(client.account).read_bytes() == before


def test_init_force_and_missing_credentials(client, monkeypatch):
    monkeypatch.setattr("inspire.platform.web.browser_api.get_current_user", lambda **kw: {})
    monkeypatch.setattr(client._transport, "_refresh", lambda: None)
    path = Accounts.config_path(client.account)
    path.write_text('[custom]\nkeep="yes"\n')
    client.init()
    assert "auth" not in Config._load_toml(path)
    assert client.init(force=True).changed
    data = Config._load_toml(path)
    assert "custom" not in data and data["tunnel"]["retries"] == 3
    assert "auth" not in data
    assert Accounts.current() == "alpha"


def test_add_persists_nondefault_base_url_in_loader_sections(client):
    Accounts.add(
        "custom", base_url="https://custom.example.invalid",
        proxy="http://127.0.0.1:7897", use=False,
    )
    data = Config._load_toml(Accounts.config_path("custom"))
    assert data == {
        "api": {"base_url": "https://custom.example.invalid"},
        "proxy": {
            "requests_http": "http://127.0.0.1:7897",
            "requests_https": "http://127.0.0.1:7897",
            "playwright": "http://127.0.0.1:7897",
            "rtunnel": "http://127.0.0.1:7897",
        },
    }
    with account_scope("custom"):
        config, _ = Config.from_files_and_env()
    assert config.base_url == "https://custom.example.invalid"


@pytest.mark.parametrize("kind", ["network", "transient", "transport"])
def test_account_check_retryable_failures(client, monkeypatch, kind):
    import requests
    from inspire.platform.errors import TransportError
    from inspire.platform.web.session.models import RefreshUnavailableError

    error = {"network": requests.ConnectionError("network"),
             "transient": RefreshUnavailableError("temporarily_unavailable", 503),
             "transport": TransportError("network")}[kind]
    monkeypatch.setattr("inspire.platform.web.browser_api.get_current_user", Mock(side_effect=error))
    result = client.account_info.check()
    assert result.ok is False
    assert result.oauth["refresh_retryable"] is True
