"""Offline account and initialization contracts, sharing the SDK HTTP blocker."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
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

    monkeypatch.setattr(auth, "get_web_session", forbidden)
    monkeypatch.setattr("inspire.accounts.normalize._playwright_chromium_available", forbidden)
    monkeypatch.setattr("inspire.accounts.normalize._install_playwright_chromium", forbidden)


def test_accounts_roundtrip(client, capsys):
    Accounts.remove("alpha")
    Accounts.remove("beta")
    assert Accounts.list() == ()
    assert Accounts.current() is None
    assert Accounts.add("one", username="login", password="fake") == "one"
    assert Accounts.current() == "one"
    Accounts.add("two", username="login", password="fake", use=False)
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

    Accounts.add("sdk", username="login", password='fake"password', proxy=proxy)
    args = ["add", "cli", "--username", "login", "--password", 'fake"password', "--non-interactive"]
    if proxy:
        args += ["--proxy", proxy]
    result = CliRunner().invoke(account, args)
    assert result.exit_code == 0, result.output
    assert Accounts.config_path("sdk").read_bytes() == Accounts.config_path("cli").read_bytes()


def test_overwrite_and_errors(client):
    with pytest.raises(ValidationError, match="already exists: alpha"):
        Accounts.add("alpha", username="new", password="fake")
    Accounts.add("alpha", username="new", password="fake", overwrite=True)
    assert Config._load_toml(Accounts.config_path("alpha"))["auth"]["username"] == "new"
    for call in [
        lambda: Accounts.use("missing"),
        lambda: Accounts.remove("missing"),
        lambda: Accounts.rename("missing", "new"),
        lambda: Accounts.config_path("../bad"),
    ]:
        with pytest.raises(ValidationError):
            call()


def test_credentials_create_update_without_pointer(client):
    Accounts.remove("alpha")
    Accounts.remove("beta")
    with InspireClient(username="login", password="fake") as created:
        assert created.account == "login"
    assert Accounts.current() is None
    Accounts.add("active", username="a", password="fake", use=True)
    path = Accounts.config_path("login")
    path.write_text(path.read_text() + '\n[custom]\nkeep = "yes"\n')
    before = Config._load_toml(path)
    with InspireClient.from_credentials("login", "fake", proxy="http://localhost:7897"):
        pass
    after = Config._load_toml(path)
    assert after.pop("proxy") == dict.fromkeys(
        ("requests_http", "requests_https", "playwright", "rtunnel"), "http://localhost:7897"
    )
    assert before == after
    assert Accounts.current() == "active"
    with InspireClient("login", username="login", password="fake"):
        pass
    assert "proxy" in Config._load_toml(path)


def test_login_cached(client, monkeypatch):
    refresh = Mock(side_effect=AssertionError("cached session must be reused"))
    monkeypatch.setattr(client._transport, "_refresh", refresh)
    user = Mock(return_value={"id": "u", "name": "User"})
    monkeypatch.setattr("inspire.platform.web.browser_api.get_current_user", user)
    result = client.login()
    assert isinstance(result, AccountInfo) and result.user_id == "u"
    assert user.call_args.kwargs["refresh"] is True
    refresh.assert_not_called()


@pytest.mark.parametrize("sso_success", [False, True])
@pytest.mark.parametrize("force", [False, True])
def test_login_renewal_ladder(client, monkeypatch, sso_success, force):
    session = client._transport._session
    renewed = replace(session)
    if not force:
        session.created_at = 0
    calls = []
    monkeypatch.setattr(WebSession, "load", lambda **kw: session)

    def sso(old):
        calls.append("sso")
        return renewed if sso_success else None

    def credentials(*args, **kwargs):
        calls.append("credentials")
        return renewed

    monkeypatch.setattr(auth, "renew_web_session_without_credentials", sso)
    monkeypatch.setattr(auth, "login_without_browser", credentials)
    monkeypatch.setattr(auth, "_persist", lambda *a, **kw: calls.append("persist"))
    monkeypatch.setattr("inspire.platform.web.browser_api.get_current_user", lambda **kw: {})
    assert isinstance(client.login(force=force), AccountInfo)
    assert calls == (["sso", "persist"] if sso_success else ["sso", "credentials"])


def test_login_no_cache(client, monkeypatch):
    session = client._transport._session
    client._transport._session = None
    monkeypatch.setattr(WebSession, "load", lambda **kw: None)
    login = Mock(return_value=session)
    monkeypatch.setattr(auth, "login_without_browser", login)
    monkeypatch.setattr("inspire.platform.web.browser_api.get_current_user", lambda **kw: {})
    client.login()
    login.assert_called_once_with("test", "unused", base_url=client.base_url, account="alpha")


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
    expected["api"]["base_url"] = client.base_url
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
    assert "extra_auth" not in data["auth"]
    assert data["auth"] == {"username": "test", "password": "unused"}
    assert data["api"]["base_url"] == client.base_url


def test_init_unchanged_dict_does_not_reformat(client, monkeypatch):
    monkeypatch.setattr("inspire.platform.web.browser_api.get_current_user", lambda **kw: {})
    path = Accounts.config_path(client.account)
    initial = path.read_text() + f'\n# Keep formatting\n[api]\nbase_url="{client.base_url}"\n'
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
    assert Config._load_toml(path)["auth"] == {"username": "test", "password": "unused"}
    assert client.init(force=True).changed
    data = Config._load_toml(path)
    assert "custom" not in data and data["tunnel"]["retries"] == 3
    assert data["auth"]["password"] == "unused"
    assert Accounts.current() == "alpha"


def test_add_persists_nondefault_base_url_in_loader_sections(client):
    Accounts.add(
        "custom", username="login", password="fake", base_url="https://custom.example.invalid",
        proxy="http://127.0.0.1:7897", use=False,
    )
    data = Config._load_toml(Accounts.config_path("custom"))
    assert data == {
        "auth": {"username": "login", "password": "fake"},
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
