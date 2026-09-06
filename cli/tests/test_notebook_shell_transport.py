from __future__ import annotations

import json

from click.testing import CliRunner

from inspire.cli.commands.notebook import remote_shell as shell_module
from inspire.cli.commands.notebook.transport import NotebookTransportPolicy
from inspire.cli.main import main as cli_main
from inspire.config import Config


def test_shell_uses_jupyter_when_policy_blocks_ssh(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr("inspire.cli.utils.account_option.account_exists", lambda name: name == "alice")
    config = Config(username="", password="")
    policy_calls = {}
    shell_calls = {}

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True, account=None: (config, {})),
    )
    monkeypatch.setattr(
        shell_module,
        "preflight_notebook_transport_policy",
        lambda _ctx, **kwargs: (
            policy_calls.update(kwargs)
            or NotebookTransportPolicy(
                notebook="gpu-box",
                notebook_id="nb-123",
                gpu_model="H200",
                session=object(),
            )
        ),
        raising=False,
    )
    called = {"jupyter": False}
    monkeypatch.setattr(
        shell_module.browser_api_module,
        "open_jupyter_terminal_shell",
        lambda **kwargs: (
            called.__setitem__("jupyter", True)
            or shell_calls.update(kwargs)
            or 0
        ),
        raising=False,
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "notebook",
            "shell",
            "gpu-box",
            "--workspace",
            "CPU资源空间",
            "--account",
            "alice",
            "--pick",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert called["jupyter"] is True
    assert result.output == ""
    assert policy_calls["workspace"] == "CPU资源空间"
    assert policy_calls["account"] == "alice"
    assert policy_calls["pick"] == 2
    assert shell_calls["session"] is not None


def test_shell_check_uses_jupyter_probe_when_policy_blocks_ssh(monkeypatch) -> None:  # noqa: ANN001
    config = Config(username="", password="")

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(
        shell_module,
        "preflight_notebook_transport_policy",
        lambda *_a, **_k: NotebookTransportPolicy(
            notebook="gpu-box",
            notebook_id="nb-123",
            gpu_model="H200",
        ),
        raising=False,
    )
    monkeypatch.setattr(
        shell_module.browser_api_module,
        "run_command_capture_in_notebook",
        lambda **_k: shell_module.browser_api_module.JupyterCommandResult(
            returncode=0,
            output="shell-check-ok\n",
            completed=True,
            marker="m",
        ),
        raising=False,
    )

    result = CliRunner().invoke(cli_main, ["notebook", "shell", "gpu-box", "--check"])

    assert result.exit_code == 0
    assert result.output == "OK\n"


def test_shell_check_json_is_compact(monkeypatch) -> None:  # noqa: ANN001
    config = Config(username="", password="")

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(
        shell_module,
        "preflight_notebook_transport_policy",
        lambda *_a, **_k: NotebookTransportPolicy(
            notebook="gpu-box",
            notebook_id="nb-123",
            gpu_model="H200",
        ),
        raising=False,
    )
    monkeypatch.setattr(
        shell_module.browser_api_module,
        "run_command_capture_in_notebook",
        lambda **_k: shell_module.browser_api_module.JupyterCommandResult(
            returncode=0,
            output="shell-check-ok\n",
            completed=True,
            marker="m",
        ),
        raising=False,
    )

    result = CliRunner().invoke(cli_main, ["--json", "notebook", "shell", "gpu-box", "--check"])

    assert result.exit_code == 0
    assert json.loads(result.output)["data"] == {
        "status": "success",
        "returncode": 0,
    }
    assert "transport" not in result.output
    assert "nb-123" not in result.output


def test_shell_check_uses_ssh_noninteractive_transport(monkeypatch) -> None:  # noqa: ANN001
    config = Config(username="", password="")

    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(
        shell_module,
        "preflight_notebook_transport_policy",
        lambda *_a, **_k: NotebookTransportPolicy(
            notebook="cpu-box",
            notebook_id="nb-123",
            gpu_model="",
        ),
        raising=False,
    )
    monkeypatch.setattr(
        shell_module,
        "resolve_cached_notebook_target",
        lambda *_a, **_k: None,
        raising=False,
    )

    class FakeBridge:
        name = "cpu-box"
        notebook_id = "nb-123"

    class FakeTunnelConfig:
        account = "default"

        def get_bridge(self, name=None):  # noqa: ANN001
            return FakeBridge() if name in (None, "cpu-box") else None

    monkeypatch.setattr(
        shell_module,
        "_load_tunnel_config_for_account",
        lambda _account=None: FakeTunnelConfig(),
        raising=False,
    )
    monkeypatch.setattr(shell_module, "is_tunnel_available", lambda **_k: True)

    captured: dict[str, object] = {}

    def fake_get_ssh_command_args(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return ["ssh", "cpu-box"]

    monkeypatch.setattr(shell_module, "get_ssh_command_args", fake_get_ssh_command_args)

    class FakeCompleted:
        returncode = 0
        stdout = "shell-check-ok\n"
        stderr = ""

    def fake_run(  # noqa: ANN001
        args,
        *,
        text: bool,
        capture_output: bool,
        check: bool,
        encoding: str | None = None,
        errors: str | None = None,
    ):
        captured["args"] = args
        captured["text"] = text
        captured["capture_output"] = capture_output
        captured["check"] = check
        return FakeCompleted()

    monkeypatch.setattr(shell_module.subprocess, "run", fake_run)

    result = CliRunner().invoke(cli_main, ["notebook", "shell", "cpu-box", "--check"])

    assert result.exit_code == 0
    assert result.output == "OK\n"
    assert "shell-check-ok" not in result.output
    assert captured["capture_output"] is True
    assert "shell-check-ok" in str(captured["remote_command"])
