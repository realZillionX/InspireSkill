"""Tests for `inspire notebook top`."""

from __future__ import annotations

import json
from typing import Optional

import pytest
from click.testing import CliRunner

from inspire.bridge.tunnel import BridgeProfile, TunnelConfig
from inspire.cli.commands.notebook import top as notebook_top_module
from inspire.cli.context import EXIT_API_ERROR, EXIT_CONFIG_ERROR, EXIT_VALIDATION_ERROR
from inspire.cli.main import main as cli_main


class _FakeCompletedProcess:
    def __init__(self, *, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _base_tunnel_config() -> TunnelConfig:
    config = TunnelConfig()
    config.add_bridge(
        BridgeProfile(
            name="gpu-a",
            proxy_url="https://proxy-a.example/ws/notebook/proxy/31337/",
            notebook_id="notebook-a",
        )
    )
    config.add_bridge(
        BridgeProfile(
            name="gpu-b",
            proxy_url="https://proxy-b.example/ws/notebook/proxy/31337/",
            notebook_id="notebook-b",
        )
    )
    config.add_bridge(
        BridgeProfile(
            name="manual",
            proxy_url="https://proxy-manual.example/ws/notebook/proxy/31337/",
        )
    )
    return config


def test_notebook_top_json_samples_all_notebook_backed_bridges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _base_tunnel_config()
    sampled: list[Optional[str]] = []

    def fake_run_ssh_command(*args, **kwargs):  # type: ignore[no-untyped-def]
        sampled.append(kwargs.get("bridge_name"))
        bridge_name = kwargs.get("bridge_name")
        if bridge_name == "gpu-a":
            return _FakeCompletedProcess(
                returncode=0,
                stdout="0, 70, 1000, 80000, 63\n1, 30, 2000, 80000, 64\n",
            )
        return _FakeCompletedProcess(
            returncode=0,
            stdout="0, 10, 500, 24000, 55\n",
        )

    monkeypatch.setattr(notebook_top_module, "load_tunnel_config", lambda: config)
    monkeypatch.setattr(notebook_top_module, "run_ssh_command", fake_run_ssh_command)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "notebook", "top", "--no-check"])
    assert result.exit_code == 0

    payload = json.loads(result.output)
    assert payload["success"] is True
    data = payload["data"]
    assert data["summary"] == {"total": 2, "ok": 2, "failed": 0}
    assert [item["bridge"] for item in data["items"]] == ["gpu-a", "gpu-b"]
    assert sampled == ["gpu-a", "gpu-b"]


def test_notebook_top_bridge_option_targets_one_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _base_tunnel_config()
    sampled: list[Optional[str]] = []

    def fake_run_ssh_command(*args, **kwargs):  # type: ignore[no-untyped-def]
        sampled.append(kwargs.get("bridge_name"))
        return _FakeCompletedProcess(returncode=0, stdout="0, 25, 1500, 24000, 56\n")

    monkeypatch.setattr(notebook_top_module, "load_tunnel_config", lambda: config)
    monkeypatch.setattr(notebook_top_module, "run_ssh_command", fake_run_ssh_command)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "top", "--notebook", "gpu-b", "--no-check"])
    assert result.exit_code == 0
    assert sampled == ["gpu-b"]
    assert "gpu-b" in result.output
    assert "gpu-a" not in result.output


def test_notebook_top_rejects_non_notebook_backed_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _base_tunnel_config()
    monkeypatch.setattr(notebook_top_module, "load_tunnel_config", lambda: config)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "top", "--notebook", "manual"])
    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "not notebook-backed" in result.output


def test_notebook_top_errors_when_no_notebook_backed_bridges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = TunnelConfig()
    config.add_bridge(
        BridgeProfile(
            name="manual",
            proxy_url="https://proxy-manual.example/ws/notebook/proxy/31337/",
        )
    )
    monkeypatch.setattr(notebook_top_module, "load_tunnel_config", lambda: config)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "top"])
    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "No notebook-backed tunnel profiles found" in result.output


def test_notebook_top_json_returns_api_error_when_all_targets_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = TunnelConfig()
    config.add_bridge(
        BridgeProfile(
            name="gpu-a",
            proxy_url="https://proxy-a.example/ws/notebook/proxy/31337/",
            notebook_id="notebook-a",
        )
    )

    def fake_run_ssh_command(*args, **kwargs):  # type: ignore[no-untyped-def]
        return _FakeCompletedProcess(returncode=1, stderr="ssh failed")

    monkeypatch.setattr(notebook_top_module, "load_tunnel_config", lambda: config)
    monkeypatch.setattr(notebook_top_module, "run_ssh_command", fake_run_ssh_command)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "notebook", "top", "--no-check"])
    assert result.exit_code == EXIT_API_ERROR

    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["data"]["summary"] == {"total": 1, "ok": 0, "failed": 1}
    assert payload["data"]["items"][0]["error"] == "ssh failed"


def test_notebook_top_watch_stops_cleanly_on_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = TunnelConfig()
    config.add_bridge(
        BridgeProfile(
            name="gpu-a",
            proxy_url="https://proxy-a.example/ws/notebook/proxy/31337/",
            notebook_id="notebook-a",
        )
    )

    def fake_run_ssh_command(*args, **kwargs):  # type: ignore[no-untyped-def]
        return _FakeCompletedProcess(returncode=0, stdout="0, 50, 1200, 24000, 58\n")

    def fake_sleep(_interval: float) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(notebook_top_module, "load_tunnel_config", lambda: config)
    monkeypatch.setattr(notebook_top_module, "run_ssh_command", fake_run_ssh_command)
    monkeypatch.setattr(notebook_top_module.time, "sleep", fake_sleep)

    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["notebook", "top", "--watch", "--no-check", "--interval", "0.1"]
    )
    assert result.exit_code == 0
    assert "Notebook GPU Telemetry" in result.output


def test_notebook_top_validates_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _base_tunnel_config()
    monkeypatch.setattr(notebook_top_module, "load_tunnel_config", lambda: config)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "top", "--interval", "0"])
    assert result.exit_code == EXIT_VALIDATION_ERROR
    assert "--interval must be greater than 0" in result.output
