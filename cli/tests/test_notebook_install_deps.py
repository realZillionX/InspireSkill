from __future__ import annotations

import pytest
from click.testing import CliRunner

from inspire.bridge.tunnel import BridgeProfile, TunnelConfig
from inspire.cli.commands.notebook import install_deps as install_deps_module
from inspire.cli.commands.notebook.install_deps import (
    DEFAULT_RAY_VERSION,
    install_deps_cmd,
)


def _patch_tunnel(monkeypatch: pytest.MonkeyPatch, *, alias: str = "cpu-box") -> list[dict]:
    """Stub load_tunnel_config + run_ssh_command_streaming. Returns a captured
    list of dicts describing each invocation, so tests can assert ordering."""
    bridge = BridgeProfile(name=alias, proxy_url="https://proxy.example/")
    config = TunnelConfig(bridges={alias: bridge}, default_bridge=alias)

    monkeypatch.setattr(install_deps_module, "load_tunnel_config", lambda: config)

    calls: list[dict] = []

    def _fake_run(*, command, bridge_name, timeout, **kwargs):  # noqa: ANN001
        calls.append(
            {
                "command": command,
                "bridge_name": bridge_name,
                "timeout": timeout,
                "kwargs": kwargs,
            }
        )
        return 0

    monkeypatch.setattr(install_deps_module, "run_ssh_command_streaming", _fake_run)
    return calls


def test_install_deps_requires_at_least_one_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_tunnel(monkeypatch)
    result = CliRunner().invoke(install_deps_cmd, ["cpu-box"])
    assert result.exit_code != 0
    assert "at least one of --slurm / --ray" in result.output


def test_install_deps_unknown_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        install_deps_module,
        "load_tunnel_config",
        lambda: TunnelConfig(bridges={}, default_bridge=None),
    )
    result = CliRunner().invoke(install_deps_cmd, ["missing", "--slurm"])
    assert result.exit_code != 0
    assert "No saved bridge for alias 'missing'" in result.output


def test_install_deps_runs_slurm_step(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_tunnel(monkeypatch)
    result = CliRunner().invoke(install_deps_cmd, ["cpu-box", "--slurm"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd = calls[0]["command"]
    assert "apt-get update" in cmd
    assert "apt-get install -y --no-install-recommends" in cmd
    for pkg in ("slurm-wlm", "slurm-client", "munge", "hwloc", "libpmix2"):
        assert pkg in cmd
    assert "DEBIAN_FRONTEND=noninteractive" in cmd
    assert calls[0]["bridge_name"] == "cpu-box"


def test_install_deps_runs_ray_step_with_default_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_tunnel(monkeypatch)
    result = CliRunner().invoke(install_deps_cmd, ["cpu-box", "--ray"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd = calls[0]["command"]
    assert f"ray=={DEFAULT_RAY_VERSION}" in cmd
    assert "pip install" in cmd
    assert "--break-system-packages" in cmd
    assert "pypi.tuna.tsinghua.edu.cn" in cmd


def test_install_deps_ray_step_respects_custom_index_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_tunnel(monkeypatch)
    result = CliRunner().invoke(
        install_deps_cmd,
        ["cpu-box", "--ray", "--pip-index-url", "https://pypi.org/simple"],
    )
    assert result.exit_code == 0, result.output
    cmd = calls[0]["command"]
    assert "pypi.org/simple" in cmd
    assert "tuna.tsinghua" not in cmd


def test_install_deps_ray_step_can_disable_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_tunnel(monkeypatch)
    result = CliRunner().invoke(
        install_deps_cmd,
        ["cpu-box", "--ray", "--pip-index-url", ""],
    )
    assert result.exit_code == 0, result.output
    cmd = calls[0]["command"]
    assert "--index-url" not in cmd


def test_install_deps_runs_ray_step_with_custom_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_tunnel(monkeypatch)
    result = CliRunner().invoke(
        install_deps_cmd, ["cpu-box", "--ray", "--ray-version", "2.40.0"]
    )
    assert result.exit_code == 0, result.output
    assert "ray==2.40.0" in calls[0]["command"]


def test_install_deps_runs_slurm_then_ray_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_tunnel(monkeypatch)
    result = CliRunner().invoke(install_deps_cmd, ["cpu-box", "--slurm", "--ray"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert "apt-get install" in calls[0]["command"]
    assert f"ray=={DEFAULT_RAY_VERSION}" in calls[1]["command"]


def test_install_deps_stops_on_first_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = BridgeProfile(name="cpu-box", proxy_url="https://proxy.example/")
    config = TunnelConfig(bridges={"cpu-box": bridge}, default_bridge="cpu-box")
    monkeypatch.setattr(install_deps_module, "load_tunnel_config", lambda: config)

    calls: list[dict] = []

    def _fake_run(*, command, bridge_name, timeout, **kwargs):  # noqa: ANN001
        calls.append({"command": command})
        return 100  # simulate apt failure

    monkeypatch.setattr(install_deps_module, "run_ssh_command_streaming", _fake_run)

    result = CliRunner().invoke(install_deps_cmd, ["cpu-box", "--slurm", "--ray"])
    assert result.exit_code != 0
    # Should have run only the first step (slurm), then stopped before ray.
    assert len(calls) == 1
    assert "apt-get install" in calls[0]["command"]
    # Hint should reference the alias.
    assert "notebook test" in result.output or "notebook refresh" in result.output


def test_install_deps_passes_timeout_to_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_tunnel(monkeypatch)
    result = CliRunner().invoke(
        install_deps_cmd, ["cpu-box", "--slurm", "--timeout", "1234"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["timeout"] == 1234
