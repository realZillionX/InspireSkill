"""CLI account/login lifecycle contracts with isolated storage and no network."""

from contextlib import contextmanager
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from inspire import accounts
from inspire.cli.main import main
from inspire.config import Config, DEFAULT_BASE_URL
from inspire.services.account import lifecycle
from inspire.services.account.account_config import (
    ACCOUNT_CONFIG_TEMPLATE,
    render_account_config,
    sanitize_account_config,
)

commands = importlib.import_module("inspire.cli.commands.login")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "requests.sessions.Session.send", Mock(side_effect=AssertionError("HTTP forbidden"))
    )
    monkeypatch.setattr(
        "httpx.AsyncClient.send", Mock(side_effect=AssertionError("HTTP forbidden"))
    )
    monkeypatch.setattr(commands, "stdin_is_interactive", lambda: False)
    login = Mock(
        return_value=SimpleNamespace(
            refresh_offline=False,
            login_username="session-user",
            access_expires_at=1000,
            refresh_expires_at=2000,
        )
    )
    monkeypatch.setattr(commands, "run_login", login)
    return login


@pytest.mark.parametrize("root_option", [False, True])
@pytest.mark.parametrize("custom", [False, True])
def test_login_creates_account(isolated, root_option, custom):
    args = ["--account", "new", "login"] if root_option else ["login", "--account", "new"]
    args += ["--no-browser", "--json"]
    if custom:
        args += ["--base-url", "https://platform.test", "--proxy", "http://localhost:7897"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    isolated.assert_called_once_with(
        "new", no_browser=True, cas_metadata={"cas": False, "cas_method": None}
    )
    assert accounts.default_account() == "new"
    data = Config._load_toml(accounts.account_config_path("new"))
    assert data["api"]["base_url"] == ("https://platform.test" if custom else DEFAULT_BASE_URL)
    assert "auth" not in data
    if custom:
        assert set(data["proxy"].values()) == {"http://localhost:7897"}
    else:
        assert "proxy" not in data


@pytest.mark.parametrize(
    "use,expected", [(None, "active"), ("--use", "other"), ("--no-use", "active")]
)
def test_existing_login_preserves_config_and_default(isolated, use, expected, monkeypatch):
    accounts.create_account("active", render_account_config(base_url=DEFAULT_BASE_URL, proxy=""))
    accounts.create_account("other", '# keep formatting\n[api]\nbase_url="https://old.test"\n')
    accounts.set_current_account("active")
    path = accounts.account_config_path("other")
    before = path.read_bytes()
    monkeypatch.setattr(commands, "stdin_is_interactive", lambda: True)
    args = ["login", "--account", "other", "--base-url", "https://ignored.test", "--proxy", ""]
    result = CliRunner().invoke(main, args + ([use] if use else []))
    assert result.exit_code == 0, result.output
    assert result.stderr.count("Warning:") == 1
    assert path.read_bytes() == before
    assert accounts.default_account() == expected


@pytest.mark.parametrize("command", ["login", "add"])
def test_no_use_prevents_first_activation(isolated, command):
    args = ["login", "--account", "first"] if command == "login" else ["account", "add", "first"]
    result = CliRunner().invoke(main, args + ["--no-use"])
    assert result.exit_code == 0, result.output
    assert accounts.default_account() is None


def test_login_and_add_share_creation(isolated, monkeypatch):
    shared = Mock(wraps=commands.ensure_account_and_login)
    monkeypatch.setattr(commands, "ensure_account_and_login", shared)
    runner = CliRunner()
    options = ["--base-url", "https://custom.test", "--proxy", "http://localhost:7897"]
    for args in (["login", "--account", "one"], ["account", "add", "two"]):
        result = runner.invoke(main, args + options)
        assert result.exit_code == 0, result.output
    assert shared.call_count == 2
    assert (
        accounts.account_config_path("one").read_bytes()
        == accounts.account_config_path("two").read_bytes()
    )
    result = runner.invoke(main, ["account", "add", "one"])
    assert result.exit_code != 0 and "Account already exists" in result.output
    assert isolated.call_count == 2


def test_bare_login_creates_default_account(isolated):
    result = CliRunner().invoke(main, ["login"])
    assert result.exit_code == 0, result.output
    assert accounts.account_dir("default").exists()
    assert accounts.default_account() == "default"
    assert isolated.call_args.args[0] == "default"


def test_bare_login_without_active_account_still_errors(isolated):
    CliRunner().invoke(main, ["login", "--account", "one", "--no-use"])
    assert accounts.default_account() is None
    result = CliRunner().invoke(main, ["login"])
    assert result.exit_code != 0
    assert "No active account" in result.output
    assert not accounts.account_dir("default").exists()


@pytest.mark.parametrize("command", ["logout", "remove"])
@pytest.mark.parametrize("active", [False, True])
def test_deletion_requires_confirmation_and_clears_tokens_under_lock(
    isolated, monkeypatch, command, active
):
    accounts.create_account("keep", "")
    accounts.create_account("delete", "")
    accounts.set_current_account("delete" if active else "keep")
    directory = accounts.account_dir("delete")
    for name in (
        "web_session.json",
        "bridges.json",
        "rtunnel-proxy-state.json",
        "cas_session.json",
        "cas_pending.json",
        "cas_qr.png",
    ):
        (directory / name).write_text("{}")
    locked = False
    events = []
    original_clear = lifecycle.clear_session_cache

    @contextmanager
    def lock(name):
        nonlocal locked
        assert name == "delete"
        locked = True
        events.append("lock")
        yield
        locked = False

    def clear(name):
        assert locked
        events.append("clear")
        original_clear(name)

    monkeypatch.setattr(lifecycle, "exclusive_session_refresh", lock)
    monkeypatch.setattr(lifecycle, "clear_session_cache", clear)
    args = (
        ["logout", "--account", "delete"]
        if command == "logout"
        else ["account", "remove", "delete"]
    )
    result = CliRunner().invoke(main, args)
    assert result.exit_code != 0 and "Pass --yes" in result.output
    assert directory.exists() and not events
    result = CliRunner().invoke(main, args + ["--yes"])
    assert result.exit_code == 0, result.output
    assert events == ["lock", "clear"]
    assert not directory.exists()
    assert accounts.account_dir("keep").exists()
    assert accounts.default_account() == (None if active else "keep")
    if active:
        assert not accounts.current_file().exists()


def test_legacy_add_credentials_skip_device_login(isolated):
    result = CliRunner().invoke(
        main,
        ["account", "add", "legacy", "--username", "u", "--password", "p", "--non-interactive"],
    )
    assert result.exit_code == 0, result.output
    isolated.assert_not_called()
    assert "inspire login --account legacy" in result.output
    data = Config._load_toml(accounts.account_config_path("legacy"))
    assert "auth" not in data


def test_config_writers_and_legacy_loading(isolated, monkeypatch):
    for content in (
        ACCOUNT_CONFIG_TEMPLATE,
        render_account_config(base_url=DEFAULT_BASE_URL, proxy=""),
    ):
        assert "[auth]" not in content
        assert "username" not in content and "password" not in content
    raw = {"auth": {"username": "old", "password": "unused", "future": True}}
    assert sanitize_account_config(raw) == {"auth": {"future": True}}
    accounts.create_account("old", '[auth]\nusername="legacy"\npassword="unused"\n')
    monkeypatch.setenv("INSPIRE_USERNAME", "environment-user")
    monkeypatch.setenv("INSPIRE_PASSWORD", "environment-password")
    config, sources = Config.from_files_and_env(account="old")
    assert config.username == "" and config.password == ""
    assert "username" not in sources and "password" not in sources
