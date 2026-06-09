from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from inspire.bridge.tunnel import BridgeProfile, TunnelConfig
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_SUCCESS
from inspire.cli.main import main as cli_main

target_resolver = importlib.import_module("inspire.cli.commands.notebook.target_resolver")
ssh_tunnel_module = importlib.import_module("inspire.bridge.tunnel")
ssh_proxy_module = importlib.import_module("inspire.cli.commands.notebook.ssh_proxy_cmd")


def _bridge(
    name: str,
    *,
    workspace: str = "CPU资源空间",
    notebook_id: str | None = None,
) -> BridgeProfile:
    return BridgeProfile(
        name=name,
        proxy_url=f"https://proxy.invalid/{name}/proxy/31337/",
        notebook_name=name,
        notebook_id=notebook_id or f"notebook-{name}",
        workspace_name=workspace,
    )


def _config(account: str, *bridges: BridgeProfile) -> TunnelConfig:
    config = TunnelConfig(account=account)
    for bridge in bridges:
        config.add_bridge(bridge)
    return config


def _install_accounts(
    monkeypatch: pytest.MonkeyPatch,
    configs: dict[str, TunnelConfig],
    *,
    current: str | None = "active",
) -> None:
    monkeypatch.setattr(target_resolver, "current_account", lambda: current)
    monkeypatch.setattr(target_resolver, "list_accounts", lambda: sorted(configs))
    monkeypatch.setattr(target_resolver, "account_exists", lambda name: name in configs)

    def fake_load_tunnel_config(account: str | None = None) -> TunnelConfig:
        if account is None:
            if current and current in configs:
                return configs[current]
            return TunnelConfig()
        return configs[account]

    monkeypatch.setattr(ssh_tunnel_module, "load_tunnel_config", fake_load_tunnel_config)


def test_resolver_finds_unique_cross_account_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _install_accounts(monkeypatch, {"alice": _config("alice", _bridge("dev-a"))})

    target = target_resolver.resolve_cached_notebook_target(
        Context(),
        notebook="dev-a",
        workspace=None,
    )

    assert target is not None
    assert target.account == "alice"
    assert target.bridge.name == "dev-a"
    cache = json.loads((tmp_path / ".inspire" / "notebook-targets.json").read_text())
    assert cache["targets"]["dev-a|workspace="]["account"] == "alice"


def test_resolver_lists_candidates_when_noninteractive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _install_accounts(
        monkeypatch,
        {
            "alice": _config("alice", _bridge("dev-box")),
            "bob": _config("bob", _bridge("dev-box")),
        },
    )

    with pytest.raises(SystemExit) as exc:
        target_resolver.resolve_cached_notebook_target(
            Context(),
            notebook="dev-box",
            workspace=None,
            allow_prompt=False,
        )

    assert exc.value.code == EXIT_CONFIG_ERROR
    err = capsys.readouterr().err
    assert "Multiple cached notebook connections match 'dev-box'" in err
    assert "account=alice" in err
    assert "account=bob" in err


def test_resolver_prompt_choice_is_cached_and_can_be_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _install_accounts(
        monkeypatch,
        {
            "alice": _config("alice", _bridge("dev-box")),
            "bob": _config("bob", _bridge("dev-box")),
        },
    )
    monkeypatch.setattr(target_resolver, "_can_prompt", lambda ctx: True)
    choices = iter([2, 1])
    monkeypatch.setattr(target_resolver.click, "prompt", lambda *args, **kwargs: next(choices))

    selected = target_resolver.resolve_cached_notebook_target(
        Context(),
        notebook="dev-box",
        workspace=None,
        allow_prompt=True,
    )
    assert selected is not None
    assert selected.account == "bob"

    cached = target_resolver.resolve_cached_notebook_target(
        Context(),
        notebook="dev-box",
        workspace=None,
        verify_target_cache=False,
        allow_prompt=True,
    )
    assert cached is not None
    assert cached.source == "target_cache"
    assert cached.account == "bob"

    ignored = target_resolver.resolve_cached_notebook_target(
        Context(),
        notebook="dev-box",
        workspace=None,
        ignore_target_cache=True,
        allow_prompt=True,
    )
    assert ignored is not None
    assert ignored.account == "alice"


def test_resolver_reselects_when_cached_target_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    configs = {
        "alice": _config("alice", _bridge("dev-box")),
        "bob": _config("bob", _bridge("dev-box")),
    }
    _install_accounts(monkeypatch, configs)
    target_resolver.remember_notebook_target(
        notebook="dev-box",
        workspace=None,
        account="alice",
        bridge=configs["alice"].get_bridge("dev-box"),
    )

    monkeypatch.setattr(target_resolver, "_can_prompt", lambda ctx: True)
    monkeypatch.setattr(target_resolver.click, "prompt", lambda *args, **kwargs: 2)
    monkeypatch.setattr(
        ssh_tunnel_module,
        "is_tunnel_available",
        lambda **kwargs: kwargs["config"].account != "alice",
    )

    selected = target_resolver.resolve_cached_notebook_target(
        Context(),
        notebook="dev-box",
        workspace=None,
        verify_target_cache=True,
        allow_prompt=True,
    )

    assert selected is not None
    assert selected.account == "bob"


def test_ssh_config_proxy_command_pins_resolved_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _install_accounts(
        monkeypatch,
        {"alice": _config("alice", _bridge("dev-box", workspace="CPU资源空间"))},
    )

    result = CliRunner().invoke(cli_main, ["notebook", "ssh-config", "dev-box"])

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "ProxyCommand inspire notebook ssh-proxy %h --account alice" in result.output


def test_ssh_proxy_explicit_account_uses_that_tunnel_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _install_accounts(
        monkeypatch,
        {"alice": _config("alice", _bridge("dev-box", workspace="CPU资源空间"))},
    )
    captured: dict[str, Any] = {}

    monkeypatch.setattr(ssh_proxy_module, "is_tunnel_available", lambda **kwargs: True)

    def fake_exec_rtunnel_proxy(*args: Any, **kwargs: Any) -> None:
        captured["config"] = args[1]
        captured["bridge"] = args[0]
        captured["kwargs"] = kwargs

    monkeypatch.setattr(ssh_proxy_module, "exec_rtunnel_proxy", fake_exec_rtunnel_proxy)

    result = CliRunner().invoke(
        cli_main,
        ["notebook", "ssh-proxy", "dev-box", "--account", "alice"],
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert captured["config"].account == "alice"
    assert captured["bridge"].name == "dev-box"
