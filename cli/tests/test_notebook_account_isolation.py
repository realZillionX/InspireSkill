from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from inspire.accounts import storage
from inspire.bridge import tunnel
from inspire.cli.commands.notebook import remote_exec, remote_shell, target_resolver, transport
from inspire.cli.context import Context
from inspire.cli.main import main


@pytest.fixture
def two_accounts(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    for account in ("alice", "bob"):
        directory = tmp_path / ".inspire" / "accounts" / account
        directory.mkdir(parents=True)
        (directory / "config.toml").write_text(
            f'[auth]\nusername = "{account}"\npassword = "test"\n'
            f'[remote_env]\nACCOUNT_ENV = "{account}"\n'
        )
    current = tmp_path / ".inspire" / "current"
    current.write_text("alice\n")
    for name in ("current_account", "list_accounts", "account_exists"):
        monkeypatch.setattr(target_resolver, name, getattr(storage, name))
    return current


def cache_connection(account):
    config = tunnel.load_tunnel_config(account=account)
    config.add_bridge(tunnel.BridgeProfile(
        name="dev-box",
        notebook_name="dev-box",
        notebook_id=f"notebook-{account}",
        workspace_name="CPU资源空间",
        proxy_url="https://example.test/proxy/31337/",
    ))
    tunnel.save_tunnel_config(config)


def stub_platform(monkeypatch, gpu_model):
    session = SimpleNamespace(account="bob")
    lookups = []

    def require_session(_ctx, *, hint, account=None):
        assert account == "bob", "preflight must authenticate as the target account"
        return session

    def base_url(*, account=None):
        assert account == "bob"
        return "https://example.test"

    def resolve(_ctx, **kwargs):
        assert kwargs["session"] is session
        lookups.append(kwargs)
        return "notebook-bob", "workspace-cpu", "cpu-group"

    monkeypatch.setattr(transport, "require_web_session", require_session)
    monkeypatch.setattr(transport, "get_base_url", base_url)
    monkeypatch.setattr(transport, "_resolve_notebook_target", resolve)
    monkeypatch.setattr(transport, "notebook_gpu_model", lambda **_kwargs: gpu_model)
    monkeypatch.setattr(tunnel, "is_tunnel_available", lambda **_kwargs: True)
    monkeypatch.setattr(
        "inspire.config.workspaces.resolve_workspace_query_scope",
        lambda **_kwargs: (["workspace-cpu"], {}),
    )
    return session, lookups


@pytest.mark.parametrize("selector", ["bob", None, "all"])
@pytest.mark.parametrize("gpu_model", ["", "H200"])
def test_exec_uses_one_account_for_preflight_environment_and_execution(
    monkeypatch, two_accounts, selector, gpu_model,
):
    cache_connection("bob")
    session, _ = stub_platform(monkeypatch, gpu_model)
    commands = []

    def run_ssh(_ctx, **kwargs):
        assert kwargs["tunnel_account"] == "bob"
        assert kwargs["config"].remote_env == {"ACCOUNT_ENV": "bob"}
        commands.append(kwargs["env_exports"])
        return 0

    def run_jupyter(_ctx, **kwargs):
        assert kwargs["session"] is session
        commands.append(kwargs["env_exports"])
        return 0

    monkeypatch.setattr(remote_exec, "try_exec_via_ssh_tunnel", run_ssh)
    monkeypatch.setattr(remote_exec, "try_exec_via_jupyter_terminal", run_jupyter)
    monkeypatch.setattr(remote_exec, "_should_auto_passthrough_stdin", lambda: False)
    args = ["notebook", "exec", "dev-box"]
    if selector:
        args += ["--account", selector]
    result = CliRunner().invoke(main, [*args, "hostname"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert len(commands) == 1
    assert "bob" in commands[0]
    assert "alice" not in commands[0]
    assert two_accounts.read_text() == "alice\n"


@pytest.mark.parametrize("selector", ["bob", None, "all"])
def test_jupyter_shell_loads_the_target_accounts_environment(
    monkeypatch, two_accounts, selector,
):
    cache_connection("bob")
    session, _ = stub_platform(monkeypatch, "H200")
    calls = []

    def run_shell(**kwargs):
        assert kwargs["session"] is session
        calls.append(kwargs["env_exports"])
        return 0

    monkeypatch.setattr(remote_shell.browser_api_module, "open_jupyter_terminal_shell", run_shell)
    args = ["notebook", "shell", "dev-box"]
    if selector:
        args += ["--account", selector]
    result = CliRunner().invoke(main, args)

    assert result.exit_code == 0, (result.output, result.exception)
    assert len(calls) == 1
    assert "bob" in calls[0]
    assert "alice" not in calls[0]
    assert two_accounts.read_text() == "alice\n"


def test_cross_account_pick_is_consumed_before_live_lookup(monkeypatch, two_accounts):
    cache_connection("alice")
    cache_connection("bob")
    _, lookups = stub_platform(monkeypatch, "")

    policy = transport.preflight_notebook_transport_policy(
        Context(), notebook="dev-box", workspace=None, account="all", pick=2,
    )

    assert policy.account == "bob"
    assert policy.cached_target.account == "bob"
    assert len(lookups) == 1
    assert lookups[0]["pick"] is None
    assert lookups[0]["workspace_ids"] == ["workspace-cpu"]
    assert two_accounts.read_text() == "alice\n"
