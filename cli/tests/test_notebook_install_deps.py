from __future__ import annotations

import pytest
from click.testing import CliRunner

from inspire.bridge.tunnel import BridgeProfile, TunnelConfig
from inspire.cli.commands.notebook import install_deps as install_deps_module
from inspire.cli.commands.notebook.install_deps import (
    DEFAULT_RAY_VERSION,
    SUPPORTED_DISTROS,
    install_deps_cmd,
)


def _patch_tunnel(monkeypatch: pytest.MonkeyPatch, *, alias: str = "cpu-box") -> list[dict]:
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


def test_slurm_step_includes_distro_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_tunnel(monkeypatch)
    result = CliRunner().invoke(install_deps_cmd, ["cpu-box", "--slurm"])
    assert result.exit_code == 0, result.output
    cmd = calls[0]["command"]
    # Distro preflight: must read /etc/os-release codename and reject anything
    # that isn't in SUPPORTED_DISTROS.
    assert "VERSION_CODENAME" in cmd
    for codename in SUPPORTED_DISTROS:
        assert codename in cmd
    assert "unsupported distro" in cmd


def test_slurm_step_skips_when_srun_present(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_tunnel(monkeypatch)
    result = CliRunner().invoke(install_deps_cmd, ["cpu-box", "--slurm"])
    assert result.exit_code == 0, result.output
    cmd = calls[0]["command"]
    # Should probe for srun + sbatch and short-circuit before apt.
    assert "command -v srun" in cmd
    assert "command -v sbatch" in cmd
    assert "skipping" in cmd
    # And still have the apt install line for the not-installed case.
    assert "apt-get install" in cmd
    for pkg in ("slurm-wlm", "slurm-client", "munge", "hwloc", "libpmix2"):
        assert pkg in cmd


def test_ray_step_skips_when_version_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_tunnel(monkeypatch)
    result = CliRunner().invoke(install_deps_cmd, ["cpu-box", "--ray"])
    assert result.exit_code == 0, result.output
    cmd = calls[0]["command"]
    # Probe via `pip show ray` -> Version field.
    assert "pip show ray" in cmd
    assert "/^Version:/" in cmd
    assert f'"$_have" = "{DEFAULT_RAY_VERSION}"' in cmd
    assert "skipping" in cmd
    # Real install command must still be present for the not-installed branch.
    assert f"ray=={DEFAULT_RAY_VERSION}" in cmd
    assert "--break-system-packages" in cmd


def test_ray_step_with_custom_version(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_tunnel(monkeypatch)
    result = CliRunner().invoke(
        install_deps_cmd, ["cpu-box", "--ray", "--ray-version", "2.40.0"]
    )
    assert result.exit_code == 0, result.output
    cmd = calls[0]["command"]
    assert 'ray==2.40.0' in cmd
    assert '"$_have" = "2.40.0"' in cmd


def test_ray_step_uses_tsinghua_mirror_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_tunnel(monkeypatch)
    result = CliRunner().invoke(install_deps_cmd, ["cpu-box", "--ray"])
    assert result.exit_code == 0, result.output
    cmd = calls[0]["command"]
    assert "pypi.tuna.tsinghua.edu.cn" in cmd


def test_ray_step_can_disable_index(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_tunnel(monkeypatch)
    result = CliRunner().invoke(
        install_deps_cmd, ["cpu-box", "--ray", "--pip-index-url", ""]
    )
    assert result.exit_code == 0, result.output
    cmd = calls[0]["command"]
    assert "--index-url" not in cmd


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
        return 100

    monkeypatch.setattr(install_deps_module, "run_ssh_command_streaming", _fake_run)

    result = CliRunner().invoke(install_deps_cmd, ["cpu-box", "--slurm", "--ray"])
    assert result.exit_code != 0
    assert len(calls) == 1
    assert "apt-get install" in calls[0]["command"]


def test_install_deps_passes_timeout_to_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_tunnel(monkeypatch)
    result = CliRunner().invoke(
        install_deps_cmd, ["cpu-box", "--slurm", "--timeout", "1234"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["timeout"] == 1234
