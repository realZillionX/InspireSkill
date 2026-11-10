from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from conftest import set_fake_home
from click.testing import CliRunner

from inspire.cli.context import EXIT_GENERAL_ERROR, EXIT_SUCCESS
from inspire.cli.commands.init import discover as discover_module
from inspire.cli.commands.init import errors as init_errors_module
from inspire.cli.commands.init import init_cmd as init_cmd_module
from inspire.cli.commands.init import json_report as json_report_module
from inspire.cli.main import main as cli_main
from inspire.config import Config


def test_init_template_writes_account_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.chdir(repo_dir)

    fake_home = tmp_path / "home"
    account_dir = fake_home / ".inspire" / "accounts" / "alice"
    account_dir.mkdir(parents=True)
    (account_dir / "config.toml").write_text("", encoding="utf-8")
    (fake_home / ".inspire" / "current").write_text("alice\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    account_config_path = tmp_path / "accounts" / "alice" / "config.toml"
    monkeypatch.setattr(
        Config,
        "writable_config_path",
        classmethod(lambda cls: account_config_path),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["init", "--template", "--force"])

    assert result.exit_code == EXIT_SUCCESS
    assert account_config_path.exists()
    assert "Inspire CLI Account Configuration" in account_config_path.read_text(encoding="utf-8")
    assert not (repo_dir / ".inspire").exists()


def test_init_fails_fast_when_no_active_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.chdir(repo_dir)

    monkeypatch.setattr(
        Config,
        "writable_config_path",
        classmethod(lambda cls: None),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["init", "--template", "--force"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "No active account configured. Run `inspire account add` first." in result.output
    assert not (repo_dir / ".inspire" / "config.toml").exists()


def test_init_defaults_to_discover_mode_with_active_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.chdir(repo_dir)

    account_config_path = tmp_path / "accounts" / "alice" / "config.toml"
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        Config,
        "writable_config_path",
        classmethod(lambda cls: account_config_path),
    )
    monkeypatch.setattr(
        init_cmd_module,
        "snapshot_paths",
        lambda account_path: {"account": account_path},
    )
    monkeypatch.setattr(init_cmd_module, "current_account", lambda: "alice")
    monkeypatch.setattr(init_cmd_module, "list_accounts", lambda: ["alice"])

    def fake_run_init_action(func, effective_json, force, **kwargs):  # noqa: ANN001
        calls["func"] = func
        calls["json"] = effective_json
        calls["force"] = force
        calls["kwargs"] = kwargs

    monkeypatch.setattr(init_cmd_module, "run_init_action", fake_run_init_action)
    monkeypatch.setattr(init_cmd_module, "_login_if_logged_out", lambda: None)
    monkeypatch.setattr(init_cmd_module, "emit_init_result", lambda **kwargs: None)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["init", "--force"])

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert calls["func"] is init_cmd_module._init_discover_mode
    assert calls["force"] is True
    assert calls["kwargs"]["non_interactive"] is True


def test_init_rejects_removed_select_project() -> None:
    result = CliRunner().invoke(
        cli_main,
        ["init", "--select-project", "Project One", "--force"],
    )

    assert result.exit_code == 2
    assert "No such option '--select-project'" in result.output


def test_init_bootstraps_first_account_before_discover(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.chdir(repo_dir)
    set_fake_home(monkeypatch, tmp_path)

    calls: dict[str, object] = {}
    monkeypatch.setattr("inspire.cli.commands.login.run_login", lambda *a, **kw: None)
    monkeypatch.setattr(init_cmd_module, "snapshot_paths", lambda *args, **kwargs: {})
    monkeypatch.setattr(init_cmd_module, "emit_init_result", lambda **kwargs: None)
    monkeypatch.setattr(init_cmd_module, "_stdin_is_interactive", lambda: True)

    def fake_run_init_action(func, effective_json, force, **kwargs):  # noqa: ANN001
        calls["func"] = func
        calls["kwargs"] = kwargs

    monkeypatch.setattr(init_cmd_module, "run_init_action", fake_run_init_action)
    monkeypatch.setattr(init_cmd_module, "_login_if_logged_out", lambda: None)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["init", "--force", "--username", "zillionx", "--base-url", "https://qz.sii.edu.cn"],
        input="zillionx\n\n",
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "Active account: zillionx" in result.output
    assert calls["func"] is init_cmd_module._init_discover_mode
    assert (tmp_path / ".inspire" / "current").read_text(encoding="utf-8") == "zillionx\n"
    account_config = (
        tmp_path / ".inspire" / "accounts" / "zillionx" / "config.toml"
    ).read_text(encoding="utf-8")
    assert 'username = "zillionx"' in account_config
    assert 'base_url = "https://qz.sii.edu.cn"' in account_config


def test_init_bootstrap_reprompts_empty_account_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.chdir(repo_dir)
    set_fake_home(monkeypatch, tmp_path)

    calls: dict[str, object] = {}
    monkeypatch.setattr("inspire.cli.commands.login.run_login", lambda *a, **kw: None)
    monkeypatch.setattr(init_cmd_module, "snapshot_paths", lambda *args, **kwargs: {})
    monkeypatch.setattr(init_cmd_module, "emit_init_result", lambda **kwargs: None)
    monkeypatch.setattr(init_cmd_module, "_stdin_is_interactive", lambda: True)

    def fake_run_init_action(func, effective_json, force, **kwargs):  # noqa: ANN001
        calls["func"] = func

    monkeypatch.setattr(init_cmd_module, "run_init_action", fake_run_init_action)
    monkeypatch.setattr(init_cmd_module, "_login_if_logged_out", lambda: None)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["init", "--force", "--username", "zillionx", "--base-url", "https://qz.sii.edu.cn"],
        input="\nlocal-account\n\n",
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "Account alias is required." in result.output
    assert "Active account: local-account" in result.output
    assert calls["func"] is init_cmd_module._init_discover_mode
    assert (tmp_path / ".inspire" / "current").read_text(encoding="utf-8") == (
        "local-account\n"
    )








def test_playwright_install_non_interactive_is_silent_and_captured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_sync_api = ModuleType("playwright.sync_api")

    def missing_runtime():
        raise RuntimeError("Chromium is missing")

    fake_sync_api.sync_playwright = missing_runtime  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    from inspire.platform.web.session import browser_launch

    monkeypatch.setattr(browser_launch, "playwright_install_args", lambda: ["install", "chromium"])
    monkeypatch.setattr(
        discover_module.click,
        "confirm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-interactive init must not confirm")
        ),
    )
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Downloading Chromium to /private/tmp/browser\n",
            stderr="installer warning\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    discover_module._ensure_playwright_browser(non_interactive=True)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert calls == [
        (
            [sys.executable, "-m", "playwright", "install", "chromium"],
            {
                "check": False,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            },
        )
    ]


def test_ssh_setup_non_interactive_is_silent_and_does_not_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        discover_module.click,
        "confirm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-interactive init must not confirm")
        ),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-interactive SSH setup must not spawn")
        ),
    )

    discover_module._ensure_ssh_key(non_interactive=True)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_run_init_action_json_captures_python_fd_and_child_output(
    capfd: pytest.CaptureFixture[str],
) -> None:
    def noisy_action() -> None:
        print("python stdout")
        print("python stderr", file=sys.stderr)
        os.write(1, b"fd stdout\n")
        os.write(2, b"fd stderr\n")
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import os; os.write(1, b'child stdout\\n'); "
                "os.write(2, b'child stderr\\n')",
            ],
            check=True,
        )

    init_errors_module.run_init_action(noisy_action, True)

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_run_init_action_json_discards_child_error_details(
    capfd: pytest.CaptureFixture[str],
) -> None:
    def failing_action() -> None:
        os.write(2, b"installer token=child-secret\n")
        print("Chromium installation failed.", file=sys.stderr)
        raise SystemExit(1)

    with pytest.raises(ValueError, match="Chromium installation failed") as exc_info:
        init_errors_module.run_init_action(failing_action, True)

    assert "child-secret" not in str(exc_info.value)
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""






def test_persist_prompted_credentials_updates_auth_username() -> None:
    account_data = {
        "auth": {"username": "仝", "password": "old-secret"},
        "api": {"base_url": "https://qz.sii.edu.cn"},
    }

    discover_module._persist_prompted_credentials(
        account_data=account_data,
        prompted_credentials=(
            "253108120116",
            "new-secret",
            "https://qz.sii.edu.cn",
        ),
    )

    assert account_data["auth"]["username"] == "253108120116"
    assert account_data["auth"]["password"] == "old-secret"
    assert account_data["api"]["base_url"] == "https://qz.sii.edu.cn"


def test_init_json_report_only_emits_result_and_changed_configs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.toml"
    before = json_report_module.snapshot_paths(config_path, tmp_path / "project.toml")
    config_path.write_text("[projects]\nproduction = \"模型项目\"\n", encoding="utf-8")

    json_report_module.emit_init_result(
        target_paths=[config_path],
        before=before,
        warnings=[],
        effective_json=True,
    )

    rendered = capsys.readouterr().out
    parsed = json.loads(rendered)
    payload = parsed.get("data", parsed)
    assert payload == {"status": "updated"}
    assert str(tmp_path) not in rendered



def test_init_logs_in_before_discovery_when_logged_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inspire.platform.web.session import auth
    from inspire.platform.web.session.models import AuthenticationError

    calls: list[str] = []

    def fake_get_web_session(**kwargs: object) -> object:
        calls.append("session")
        raise AuthenticationError("Not logged in; run inspire login.")

    monkeypatch.setattr(init_cmd_module, "current_account", lambda: "alice")
    monkeypatch.setattr(auth, "get_web_session", fake_get_web_session)
    monkeypatch.setattr(
        "inspire.cli.commands.login.run_login",
        lambda account, **kw: calls.append(f"login:{account}"),
    )

    init_cmd_module._login_if_logged_out()
    assert calls == ["session", "login:alice"]


def test_init_skips_login_when_session_is_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inspire.platform.web.session import auth

    def fail_login(*args: object, **kwargs: object) -> None:
        raise AssertionError("login must not run")

    monkeypatch.setattr(init_cmd_module, "current_account", lambda: "alice")
    monkeypatch.setattr(auth, "get_web_session", lambda **kwargs: object())
    monkeypatch.setattr("inspire.cli.commands.login.run_login", fail_login)
    init_cmd_module._login_if_logged_out()
