"""Local auth metadata and CLI checks never expose authentication material."""

from __future__ import annotations

import importlib
import json
import time
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from conftest import set_fake_home
from inspire.accounts import storage
from inspire.cli.context import EXIT_AUTH_ERROR
from inspire.cli.main import main
from inspire.platform.web.session.models import WebSession
from inspire.services.account.auth_status import inspect_auth_status, format_auth_status

check_module = importlib.import_module("inspire.cli.commands.account.check")


@pytest.fixture
def account_home(monkeypatch, tmp_path):
    set_fake_home(monkeypatch, tmp_path)
    for name in ("alice", "bob"):
        storage.create_account(name, '[api]\nbase_url = "https://aip.sii.edu.cn"\n')
    storage.set_current_account("alice")
    return tmp_path


def write_session(name="alice", access=3600, refresh=604800, issued=None):
    now = time.time()
    session = WebSession(
        access_token="SECRET-ACCESS",
        refresh_token="SECRET-REFRESH",
        access_expires_at=now + access,
        refresh_expires_at=now + refresh,
        created_at=issued or now,
        login_username="test-user",
        account=name,
    )
    session.save(account=name)
    return session


def write_cas(name="alice"):
    (storage.account_dir(name) / "cas_session.json").write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "CASTGC",
                        "value": "SECRET-COOKIE",
                        "domain": "cas.sii.edu.cn",
                        "path": "/",
                        "expires": None,
                        "secure": True,
                    }
                ],
                "created_at": time.time() - 120,
                "method": "feishu",
            }
        )
    )


def snapshot(home):
    return {
        str(p.relative_to(home)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in home.rglob("*")
        if p.is_file()
    }


def no_secrets(value):
    for secret in ("SECRET-ACCESS", "SECRET-REFRESH", "SECRET-COOKIE"):
        assert secret not in str(value)


@pytest.mark.parametrize(
    "access,refresh,state",
    [
        (3600, 604800, "valid"),
        (30, 600, "refreshable"),
        (-10, 600, "refreshable"),
        (-100, -10, "expired"),
    ],
)
@pytest.mark.parametrize("cas", [False, True])
def test_inspector(account_home, access, refresh, state, cas):
    write_session(access=access, refresh=refresh)
    if cas:
        write_cas()
    before = snapshot(account_home)
    result = inspect_auth_status("alice")
    assert result["oauth"]["state"] == state
    assert result["cas"]["state"] == ("present" if cas else "missing")
    assert result["oauth"]["access_expires_in"] >= 0
    assert result["oauth"]["issued_at"]
    assert ("note" in result["oauth"]) == (state == "expired" and cas)
    no_secrets(result)
    no_secrets(format_auth_status(result, "alice"))
    assert snapshot(account_home) == before


@pytest.mark.parametrize("legacy", [False, True])
def test_missing_legacy(account_home, legacy):
    if legacy:
        (storage.account_dir("alice") / "web_session.json").write_text('{"token":"SECRET-ACCESS"}')
    assert inspect_auth_status("alice")["oauth"]["state"] == ("legacy" if legacy else "missing")


@pytest.mark.parametrize("state", ["valid", "refreshable", "expired", "missing", "legacy"])
@pytest.mark.parametrize("as_json", [False, True])
def test_offline(account_home, monkeypatch, state, as_json):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline check attempted network/refresh")

    monkeypatch.setattr(check_module, "get_web_session", forbidden)
    monkeypatch.setattr(check_module, "cas_ticket_valid", forbidden)
    if state in {"valid", "refreshable", "expired"}:
        write_session(
            "bob",
            access=3600 if state == "valid" else -10,
            refresh=-10 if state == "expired" else 600,
        )
    elif state == "legacy":
        (storage.account_dir("bob") / "web_session.json").write_text("{}")
    write_cas("bob")
    before = snapshot(account_home)
    result = CliRunner().invoke(
        main,
        ["--account", "bob"] + (["--json"] if as_json else []) + ["account", "check", "--offline"],
    )
    assert result.exit_code == (0 if state in {"valid", "refreshable"} else EXIT_AUTH_ERROR), (
        result.output
    )
    assert snapshot(account_home) == before
    no_secrets(result.output)
    if state == "legacy":
        assert "Session predates OAuth login (v7.2.0)" in result.output
        assert "inspire login --account bob" in result.output
    if as_json:
        data = json.loads(result.output)["data"]
        assert data["account"] == "bob"
        assert data["authenticated"] is None
        assert data["mode"] == "offline"
        assert "refreshed" not in data
        assert data["oauth"]["state"] == state
        assert "login_username" not in data
        assert data["cas"]["login_method"] == "feishu"
    else:
        assert "not verified (offline)" in result.output
        if state == "refreshable":
            assert "refreshable by local expiry (not verified offline)" in result.output


@pytest.mark.parametrize("refreshed", [False, True])
@pytest.mark.parametrize("cas_state", ["valid", "expired", "error"])
def test_live(account_home, monkeypatch, refreshed, cas_state):
    session = write_session(issued=time.time() - 100)
    write_cas()

    def get_session():
        return write_session() if refreshed else session

    def probe(cached, account):
        assert account == "alice"
        if cas_state == "error":
            raise RuntimeError("SECRET-COOKIE")
        return cas_state == "valid"

    monkeypatch.setattr(check_module, "get_web_session", get_session)
    monkeypatch.setattr(check_module.browser_api_module, "get_current_user", lambda **kw: {})
    monkeypatch.setattr(check_module, "cas_ticket_valid", probe)
    result = CliRunner().invoke(main, ["--json", "account", "check"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["authenticated"] is True
    assert data["mode"] == "live"
    assert data["refreshed"] is refreshed
    assert data["cas"]["state"] == ("valid" if cas_state == "valid" else "expired")
    assert data["oauth"]["state"] == "valid"
    no_secrets(result.output)


def test_list(account_home, monkeypatch):
    write_session()
    (storage.account_dir("bob") / "web_session.json").write_text("{}")
    write_cas("bob")
    before = snapshot(account_home)
    for flags in ([], ["--json"]):
        result = CliRunner().invoke(main, flags + ["account", "list"])
        assert result.exit_code == 0, result.output
        no_secrets(result.output)
        if flags:
            items = json.loads(result.output)["data"]["items"]
            assert items[0]["oauth_state"] == "valid"
            assert items[0]["refresh_expires_at"]
            assert items[1]["oauth_state"] == "legacy"
            assert items[1]["cas_state"] == "present"
        else:
            assert "oauth: valid (refresh" in result.output
            assert "oauth: legacy (run inspire login --account bob)" in result.output
    assert snapshot(account_home) == before


def test_live_legacy(account_home, monkeypatch):
    (storage.account_dir("alice") / "web_session.json").write_text("{}")

    def fail():
        raise ValueError("Not logged in")

    monkeypatch.setattr(check_module, "get_web_session", fail)
    result = CliRunner().invoke(main, ["account", "check"])
    assert result.exit_code == EXIT_AUTH_ERROR
    assert "Session predates OAuth login (v7.2.0)" in result.output


def test_account_checks_skip_background_updates(account_home, monkeypatch):
    main_module = importlib.import_module("inspire.cli.main")
    calls = []
    monkeypatch.setattr(main_module, "maybe_spawn_check", lambda: calls.append("spawn"))
    monkeypatch.setattr(main_module, "maybe_notify_update", lambda: calls.append("notify"))
    for args in (["account", "check", "--offline"], ["account", "list"]):
        CliRunner().invoke(main, args)
    assert calls == []


def test_inspector_does_not_repair_identity(account_home, monkeypatch):
    session = write_session()
    session.login_username = None
    session.save(account="alice")
    before = snapshot(account_home)
    monkeypatch.setattr(WebSession, "save", lambda *a, **kw: pytest.fail("Inspector wrote session"))
    assert "login_username" not in inspect_auth_status("alice")["oauth"]
    assert snapshot(account_home) == before


def test_offline_still_validates_config(account_home):
    storage.account_config_path("alice").write_text('[api]\nbase_url = "https://api.example.com"\n')
    result = CliRunner().invoke(main, ["account", "check", "--offline"])
    from inspire.cli.context import EXIT_CONFIG_ERROR

    assert result.exit_code == EXIT_CONFIG_ERROR


@pytest.mark.parametrize("rejected", [False, True])
@pytest.mark.parametrize("as_json", [False, True])
def test_live_failure(account_home, monkeypatch, rejected, as_json):
    from inspire.platform.web.session import auth

    write_session("bob", access=-10)
    monkeypatch.setattr(auth, "_http", lambda account: MagicMock())

    def fail_refresh(*args):
        if rejected:
            raise auth.RefreshRejectedError("invalid_grant", 400)
        raise auth.AuthenticationError("Other failure")

    monkeypatch.setattr(auth.DeviceAuthorization, "refresh", fail_refresh)
    monkeypatch.setattr(check_module, "get_web_session", lambda: auth.refresh_cached("bob"))
    result = CliRunner().invoke(
        main, ["--account", "bob"] + (["--json"] if as_json else []) + ["account", "check"]
    )
    assert result.exit_code == EXIT_AUTH_ERROR, result.output
    assert "renewed automatically" not in result.output
    assert "OK Configuration" not in result.output
    assert "Error: Authentication" not in result.output
    no_secrets(result.output)
    if as_json:
        data = json.loads(result.output)["data"]
        assert data["authenticated"] is False
        if rejected:
            assert data["oauth"]["state"] == "rejected"
            assert data["oauth"]["refresh_error"] == "invalid_grant"
            assert "for example, the login session ended or was revoked" in data["authentication_error"]
    else:
        assert "Configuration: OK" in result.output
        assert "Authentication: FAILED" in result.output
        if rejected:
            assert "rejected by server (invalid_grant); local expiry" in result.output
            assert "Authentication error: Session refresh was rejected" in result.output
            assert "inspire login --account bob" in result.output


@pytest.mark.parametrize("refreshed", [False, True])
def test_live_human_success(account_home, monkeypatch, refreshed):
    session = write_session(issued=time.time() - 100)
    monkeypatch.setattr(check_module, "get_web_session", lambda: write_session() if refreshed else session)
    monkeypatch.setattr(check_module.browser_api_module, "get_current_user", lambda **kw: {})
    result = CliRunner().invoke(main, ["account", "check"])
    assert result.exit_code == 0, result.output
    assert "Configuration: OK" in result.output.splitlines()
    assert "Authentication: OK" + (" (refreshed now)" if refreshed else "") in result.output.splitlines()
    assert "OAuth refresh token: valid" in result.output


def test_local_refreshable_has_no_renewal_promise(account_home):
    write_session(access=-10)
    lines = format_auth_status(inspect_auth_status("alice"), "alice")
    assert "renewed automatically" not in "\n".join(lines)


@pytest.mark.parametrize("access", [-10, 3600])
@pytest.mark.parametrize("estimate", [-10, 2592000])
@pytest.mark.parametrize("command", ["list", "offline-check", "live-check", "rejected-check"])
@pytest.mark.parametrize("as_json", [False, True])
def test_offline_token_status(account_home, monkeypatch, access, estimate, command, as_json):
    from inspire.platform.web.session.models import RefreshRejectedError

    session = write_session(access=access, refresh=estimate)
    session.refresh_offline = True
    session.save()
    before = snapshot(account_home)
    monkeypatch.setattr(check_module, "get_web_session", lambda: session)

    def probe(**kwargs):
        if command == "rejected-check":
            raise RefreshRejectedError("invalid_grant", 400)
        return {}

    monkeypatch.setattr(check_module.browser_api_module, "get_current_user", probe)
    args = ["account", "list"] if command == "list" else ["account", "check"]
    if command == "offline-check":
        args.append("--offline")
    result = CliRunner().invoke(main, (["--json"] if as_json else []) + args)
    assert result.exit_code == (EXIT_AUTH_ERROR if command == "rejected-check" else 0), result.output
    assert snapshot(account_home) == before
    no_secrets(result.output)
    if as_json:
        data = json.loads(result.output)["data"]
        oauth = data["items"][0]["oauth"] if command == "list" else data["oauth"]
        assert oauth["refresh_kind"] == "offline"
        assert oauth["refresh_expires_at"] is None
        assert oauth["refresh_expires_in"] is None
        assert oauth["state"] == (
            "rejected" if command == "rejected-check" else "valid" if access > 60 else "offline"
        )
    elif command == "rejected-check":
        assert "rejected by server (invalid_grant)" in result.output
        assert "local expiry" not in result.output
    else:
        assert "no fixed expiry; server-side idle timeout applies" in result.output
        assert "refreshable" not in result.output


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("offline_token", [False, True])
def test_live_transient_failure(account_home, monkeypatch, as_json, offline_token):
    from inspire.cli.context import EXIT_API_ERROR
    from inspire.platform.web.session import auth, RefreshUnavailableError

    cached = write_session("bob", access=-10)
    cached.refresh_offline = offline_token
    cached.save()
    error = RefreshUnavailableError("temporarily_unavailable", 503)
    assert isinstance(error, ValueError)
    monkeypatch.setattr(auth.DeviceAuthorization, "refresh", MagicMock(side_effect=error))
    monkeypatch.setattr(check_module, "get_web_session", lambda: auth.refresh_cached("bob"))
    result = CliRunner().invoke(
        main, ["--account", "bob"] + (["--json"] if as_json else []) + ["account", "check"]
    )
    assert result.exit_code == EXIT_API_ERROR, result.output
    no_secrets(result.output)
    assert "rejected" not in result.output
    assert "retry later" in result.output
    if as_json:
        data = json.loads(result.output)["data"]
        assert data["authenticated"] is False
        assert data["oauth"]["state"] == ("offline" if offline_token else "refreshable")
        assert data["oauth"]["refresh_error"] == "temporarily_unavailable"
        assert data["oauth"]["refresh_retryable"] is True
        assert data["oauth"]["refresh_status"] == 503
    else:
        assert "OAuth refresh: temporarily unavailable (HTTP 503); retry later" in result.output
        assert "Authentication: FAILED" in result.output
