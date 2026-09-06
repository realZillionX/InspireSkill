from __future__ import annotations

import importlib
import gc
import json
import os
import time
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from inspire.accounts import storage
from inspire.bridge import tunnel
from inspire.cli.commands.notebook import target_resolver
from inspire.cli.context import Context
from inspire.cli.main import main
from inspire.cli.utils.resource_index import resource_index_path
from inspire.config import Config
from inspire.platform.web import session as web_session
from inspire.platform.web.browser_api import core
from multiprocess_workers import adopt_home, run_workers, worker_context


def prepare_accounts():
    for account in ("alice", "bob"):
        storage.create_account(account, (
            f'[auth]\nusername = "{account}"\npassword = "test"\n'
            f'[api]\nbase_url = "https://{account}.example"\n'
            f'[proxy]\nrequests_https = "http://{account}.proxy:8080"\n'
            f'[remote_env]\nACCOUNT_ENV = "{account}"\n'
        ))
        web_session.WebSession(
            storage_state={"cookies": [{"name": "test", "value": account}]},
            created_at=time.time(), login_username=account, account=account,
            base_url=f"https://{account}.example",
        ).save(account=account)
        config = tunnel.load_tunnel_config(account=account)
        config.add_bridge(tunnel.BridgeProfile(
            name="same-name", notebook_name="same-name", notebook_id=f"notebook-{account}",
            workspace_name="CPU资源空间", proxy_url=f"https://{account}.example/proxy/31337/",
        ))
        tunnel.save_tunnel_config(config)
        for filename in ("rtunnel-proxy-state.json", "resource-index.sqlite3"):
            (storage.account_dir(account) / filename).write_bytes(f"{account}:{filename}".encode())
    storage.set_current_account("alice")


@pytest.fixture
def accounts(monkeypatch, tmp_path, request):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("INSPIRE_BASE_URL", raising=False)
    request.getfixturevalue("active_account_session_storage")
    for name in ("current_account", "account_exists"):
        monkeypatch.setattr(target_resolver, name, getattr(storage, name))
    prepare_accounts()
    return tmp_path


def assert_account_runtime(account):
    assert storage.current_account() == account
    config, _ = Config.from_files_and_env()
    assert config.username == account
    assert config.remote_env == {"ACCOUNT_ENV": account}
    assert config.requests_https_proxy == f"http://{account}.proxy:8080"
    assert core._get_base_url() == f"https://{account}.example"
    session = web_session.get_web_session()
    assert session.account == account
    assert session.login_username == account
    assert tunnel.load_tunnel_config().account == account
    assert resource_index_path().parent.name == account


@pytest.mark.parametrize("position", ["root", "group", "leaf", "default"])
def test_account_selection_pins_the_whole_command(monkeypatch, accounts, position):
    expected = "alice" if position == "default" else "bob"
    called = []

    def inspect_runtime(**_kwargs):
        assert_account_runtime(expected)
        # A different invocation may change the persistent default mid-command.
        storage.set_current_account("bob" if expected == "alice" else "alice")
        assert_account_runtime(expected)
        called.append(True)

    monkeypatch.setattr(main.commands["job"].commands["list"], "callback", inspect_runtime)
    args = ["job", "list", "--workspace", "CPU资源空间"]
    position_index = {"root": 0, "group": 1, "leaf": 2, "default": None}[position]
    if position_index is not None:
        args[position_index:position_index] = ["--account", "bob"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, (result.output, result.exception)
    assert called == [True]
    assert storage.current_account() == storage.default_account()


def test_all_commands_expose_the_shared_account_selector():
    from inspire.cli.utils.account_option import _select_account

    def check(command):
        options = [p for p in command.params if "--account" in p.opts]
        assert len(options) == 1, command.name
        assert options[0].callback is _select_account
        if isinstance(command, click.Group):
            for child in command.commands.values():
                check(child)

    check(main)


@pytest.mark.parametrize("args", [
    ["--json", "--account", "all", "job", "list"],
    ["--account", "all", "--json", "job", "list"],
    ["--json", "notebook", "exec", "same-name", "--account", "all", "hostname"],
])
def test_unknown_account_is_not_a_wildcard(monkeypatch, accounts, args):
    monkeypatch.setattr(web_session, "get_web_session", lambda **_k: pytest.fail("no login"))
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 10
    assert json.loads(result.output)["error"]["type"] == "ConfigError"
    assert storage.default_account() == "alice"


@pytest.mark.parametrize("args", [
    ["--account", "bob", "job", "list"],
    ["job", "--account", "bob", "list"],
    ["job", "list", "--account", "bob"],
])
def test_failed_parse_does_not_leak_the_command_account(accounts, args):
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 2  # --workspace is required.
    assert storage.current_account() == "alice"
    assert storage.default_account() == "alice"
    storage.set_current_account("bob")
    del result
    gc.collect()
    assert storage.current_account() == "bob"


def test_command_exception_restores_the_callers_account(monkeypatch, accounts):
    def fail(**_kwargs):
        assert storage.current_account() == "bob"
        raise RuntimeError("test failure")

    monkeypatch.setattr(main.commands["job"].commands["list"], "callback", fail)
    result = CliRunner().invoke(main, [
        "--account", "bob", "job", "list", "--workspace", "CPU资源空间",
    ])
    assert result.exit_code == 1
    assert storage.current_account() == "alice"
    assert storage.default_account() == "alice"


def test_account_use_keeps_every_accounts_caches_and_ssh_profiles(monkeypatch, accounts):
    snapshots = {
        p: p.read_bytes() for p in storage.accounts_dir().rglob("*") if p.is_file()
    }
    for account in ("bob", "alice"):
        result = CliRunner().invoke(main, ["account", "use", account])
        assert result.exit_code == 0, result.output
        assert storage.default_account() == account
        assert_account_runtime(account)
        for path, content in snapshots.items():
            assert path.read_bytes() == content
        result = CliRunner().invoke(main, ["notebook", "ssh-config", "same-name"])
        assert result.exit_code == 0, result.output
        assert f"--account {account}" in result.output


def test_default_account_does_not_follow_another_accounts_remembered_target(monkeypatch, accounts):
    bob = tunnel.load_tunnel_config(account="bob").get_bridge("same-name")
    # Simulate the existing version-1 on-disk format without discarding it.
    target_resolver.target_cache_path().write_text(json.dumps({
        "version": 1,
        "targets": {"same-name|workspace=": {
            "account": "bob", "bridge_name": bob.name, "notebook_name": bob.notebook_name,
            "notebook_id": bob.notebook_id,
        }},
    }))
    selected = target_resolver.resolve_cached_notebook_target(
        Context(), notebook="same-name", workspace=None, verify_target_cache=False,
    )
    assert selected.account == "alice"
    selected = target_resolver.resolve_cached_notebook_target(
        Context(), notebook="same-name", workspace=None, account="bob", verify_target_cache=False,
    )
    assert selected.account == "bob"
    assert selected.source == "target_cache"
    entries = target_resolver._read_target_cache()["targets"]
    assert set(entries) == {
        "same-name|workspace=|account=alice", "same-name|workspace=|account=bob",
    }
    target_resolver.forget_notebook_targets(notebook="same-name")
    assert set(target_resolver._read_target_cache()["targets"]) == {
        "same-name|workspace=|account=bob",
    }


def test_internal_worker_thread_keeps_selected_account(accounts):
    with storage.account_scope("bob"):
        core._run_in_thread(assert_account_runtime, "bob")
    assert storage.current_account() == "alice"


def _concurrent_command(index, home, barrier):
    adopt_home(home)
    os.chdir(home)
    os.environ["INSPIRE_SKIP_UPDATE_CHECK"] = "1"
    os.environ.pop("INSPIRE_BASE_URL", None)
    cli_module = importlib.import_module("inspire.cli.main")
    cli_module.maybe_notify_update = lambda: None
    cli_module.maybe_spawn_check = lambda: None
    expected = "alice" if index == 0 else "bob"

    def inspect_runtime(**_kwargs):
        assert_account_runtime(expected)
        barrier.wait(timeout=15)
        if index == 1:
            storage.set_current_account("bob")
        barrier.wait(timeout=15)
        assert_account_runtime(expected)

    main.commands["job"].commands["list"].callback = inspect_runtime
    args = ["job", "list", "--workspace", "CPU资源空间"] if index == 0 else ["--account", "bob", "job", "list", "--workspace", "CPU资源空间"]
    main.main(args=args, standalone_mode=False)


def test_concurrent_accounts_survive_a_default_switch(accounts):
    context = worker_context()
    barrier = context.Barrier(2)
    snapshots = {
        p: p.read_bytes() for p in storage.accounts_dir().rglob("*") if p.is_file()
    }
    codes = run_workers(
        context, _concurrent_command, count=2,
        args_for=lambda index: (index, str(accounts), barrier),
    )
    assert codes == [0, 0]
    assert storage.default_account() == "bob"
    for path, content in snapshots.items():
        assert path.read_bytes() == content
