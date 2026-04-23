"""Tests for notebook rtunnel shell command construction."""

from __future__ import annotations

import pytest

from inspire.config.ssh_runtime import SshRuntimeConfig
from inspire.platform.web.browser_api.rtunnel import (
    BOOTSTRAP_SENTINEL,
    build_rtunnel_setup_commands,
)


def test_build_commands_uses_explicit_runtime_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSPIRE_RTUNNEL_BIN", "/env/rtunnel")
    monkeypatch.setenv("INSPIRE_SSHD_DEB_DIR", "/env/sshd")

    runtime = SshRuntimeConfig(
        rtunnel_bin="/project/rtunnel",
        sshd_deb_dir="/project/sshd",
        rtunnel_download_url="https://project.example/rtunnel.tgz",
    )
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    assert "RTUNNEL_BIN_PATH=/project/rtunnel" in joined
    assert "SSHD_DEB_DIR=/project/sshd" in joined
    assert "/env/rtunnel" not in joined
    assert "/env/sshd" not in joined
    # With sshd_deb_dir set, dpkg -i should be used for .deb installation
    assert "dpkg -i" in joined
    # Shell snippet sets RTUNNEL_DOWNLOAD_URL dynamically
    assert "RTUNNEL_DOWNLOAD_URL=" in joined
    # RTUNNEL_URL compat alias references RTUNNEL_DOWNLOAD_URL
    assert 'RTUNNEL_URL="$RTUNNEL_DOWNLOAD_URL"' in joined


def test_dropbear_without_setup_script_uses_dpkg() -> None:
    """When dropbear_deb_dir is set but setup_script is not, the internal
    dpkg-based installation should be used instead of raising ValueError."""
    runtime = SshRuntimeConfig(
        dropbear_deb_dir="/project/dropbear",
        setup_script=None,
    )

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    # Should contain the DROPBEAR_DEB_DIR variable
    assert "DROPBEAR_DEB_DIR=" in joined
    # Should contain dpkg -i fallback for raw .deb packages
    assert "dpkg -i" in joined
    # Should NOT curl rtunnel binary (offline notebook with dropbear config)
    assert "curl -fsSL" not in joined
    # Should emit error message when rtunnel binary not found
    assert "no curl fallback for offline notebooks" in joined
    # Should NOT contain SETUP_SCRIPT (no external script)
    assert "SETUP_SCRIPT=" not in joined


def test_dropbear_apt_mirror_fallback() -> None:
    """When apt_mirror_url is set, the bootstrap should fall back to
    apt-get install dropbear-bin if dpkg fails."""
    runtime = SshRuntimeConfig(
        dropbear_deb_dir="/project/dropbear",
        apt_mirror_url="http://nexus.example/repository/ubuntu/",
    )

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    assert "APT_MIRROR_URL=" in joined
    assert "apt-get install -y -qq dropbear-bin" in joined
    assert "inspire-mirror.list" in joined
    # dpkg path should still be tried first
    assert "dpkg -i" in joined
    # Codename detection via /etc/os-release (primary) then lsb_release (fallback)
    assert "/etc/os-release" in joined
    assert "VERSION_CODENAME" in joined
    assert "lsb_release" in joined
    # Existing sources moved aside to avoid timeout on unreachable mirrors
    assert "sources.list.bak" in joined
    # Dropbear launch should be guarded by host key existence
    assert "[ -f /tmp/dropbear_ed25519_host_key ]" in joined


def test_apt_mirror_only_without_dropbear_deb_dir() -> None:
    """When only apt_mirror_url is set (no dropbear_deb_dir), the dropbear
    path should still be entered and apt install should run."""
    runtime = SshRuntimeConfig(
        apt_mirror_url="http://nexus.example/repository/ubuntu/",
    )

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    assert "APT_MIRROR_URL=" in joined
    assert "apt-get install -y -qq dropbear-bin" in joined
    # Should use dropbear path (not openssh)
    assert "dropbear" in joined
    # Should NOT have DROPBEAR_DEB_DIR set
    assert "DROPBEAR_DEB_DIR=" not in joined


def test_dropbear_command_contains_setup_script_and_args() -> None:
    runtime = SshRuntimeConfig(
        rtunnel_bin="/project/rtunnel",
        dropbear_deb_dir="/project/dropbear",
        setup_script="/project/setup_ssh.sh",
    )

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key="ssh-ed25519 AAAA... test@example",
        ssh_runtime=runtime,
    )

    joined = "\n".join(commands)
    assert any(line.startswith("DROPBEAR_DEB_DIR=/project/dropbear") for line in commands)
    assert any(line.startswith("SETUP_SCRIPT=/project/setup_ssh.sh") for line in commands)
    assert "falling back to openssh bootstrap" in joined
    assert "RTUNNEL_URL=" in joined
    assert 'RTUNNEL_URL="$RTUNNEL_DOWNLOAD_URL"' in joined
    assert 'if [ ! -f "$BOOTSTRAP_SENTINEL" ] || [ ! -x /tmp/rtunnel ]; then ' in joined
    assert 'bash "$SETUP_SCRIPT" "$DROPBEAR_DEB_DIR" "$RTUNNEL_BIN_PATH"' in joined
    assert "apt-get install -y -qq openssh-server" in joined
    assert 'grep -q "[s]shd -p $SSH_PORT"' in joined
    assert 'rm -f "$BOOTSTRAP_SENTINEL"' in joined
    # Verify the long single-line command is gone — setup invocation should be its own line
    assert not any(
        ">/tmp/setup_ssh.log 2>&1; tail" in line for line in commands
    ), "setup + tail should be separate commands, not chained with ;"


def test_non_dropbear_uses_bootstrap_sentinel_and_start_only_commands() -> None:
    runtime = SshRuntimeConfig(
        rtunnel_bin="/project/rtunnel",
        dropbear_deb_dir=None,
    )

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    assert f"BOOTSTRAP_SENTINEL={BOOTSTRAP_SENTINEL}" in joined
    assert 'if [ ! -f "$BOOTSTRAP_SENTINEL" ] || [ ! -x /tmp/rtunnel ] ' in joined
    assert "apt-get install -y -qq openssh-server" in joined
    assert 'touch "$BOOTSTRAP_SENTINEL"' in joined
    assert 'rm -f "$BOOTSTRAP_SENTINEL"' in joined
    assert "pkill -f 'sshd -p'" not in joined
    assert 'pkill -f "rtunnel.*:$PORT"' not in joined
    assert 'grep -q "[s]shd -p ' in joined
    assert 'grep -Eq "[r]tunnel .*([[:space:]]|:)$PORT([[:space:]]|$)"' in joined
    # Shell snippet sets RTUNNEL_DOWNLOAD_URL dynamically
    assert "RTUNNEL_DOWNLOAD_URL=" in joined
    # RTUNNEL_URL compat alias
    assert 'RTUNNEL_URL="$RTUNNEL_DOWNLOAD_URL"' in joined
    # Curl block uses $RTUNNEL_DOWNLOAD_URL (not a literal URL)
    assert '"$RTUNNEL_DOWNLOAD_URL" -o /tmp/rtunnel.tgz' in joined


# ---------------------------------------------------------------------------
# Container-preinstalled rtunnel probe
#
# The canonical rtunnel source is baking the binary into the notebook image
# (unified-base:v1 installs it at /usr/local/bin/rtunnel; derived images
# inherit it, or can add it during their build). The bootstrap script must
# locate that copy before falling through to the curl download, because curl
# is unreachable on most offline GPU compute groups.
# ---------------------------------------------------------------------------


def test_preinstalled_rtunnel_probe_always_emitted() -> None:
    runtime = SshRuntimeConfig()
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    assert "command -v rtunnel" in joined
    assert '_inspire_preinstalled_rt=' in joined
    assert 'cp "$_inspire_preinstalled_rt" /tmp/rtunnel' in joined


def test_preinstalled_probe_runs_before_arch_validation() -> None:
    runtime = SshRuntimeConfig()
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )

    probe_idx = None
    arch_idx = None
    for i, line in enumerate(commands):
        if probe_idx is None and "command -v rtunnel" in line:
            probe_idx = i
        if arch_idx is None and "! /tmp/rtunnel --help" in line:
            arch_idx = i

    assert probe_idx is not None
    assert arch_idx is not None
    assert probe_idx < arch_idx, (
        "Preinstalled-rtunnel probe must run before the arch validator, so a "
        "bad preinstalled binary still gets cleaned up before bootstrap continues."
    )


# ---------------------------------------------------------------------------
# RTUNNEL_MISSING marker (bootstrap diagnostic)
# ---------------------------------------------------------------------------


def test_bootstrap_emits_missing_marker_when_rtunnel_absent() -> None:
    """After the setup script finishes, if /tmp/rtunnel still isn't there
    the script must echo the well-known marker so the CLI can surface a
    structured "bake rtunnel into your image" error instead of letting the
    user sit through the 120s proxy-verify timeout."""
    from inspire.platform.web.browser_api.rtunnel import RTUNNEL_MISSING_MARKER

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=SshRuntimeConfig(),
    )
    joined = "\n".join(commands)
    assert RTUNNEL_MISSING_MARKER in joined
    assert "if [ ! -x /tmp/rtunnel ]" in joined


# ---------------------------------------------------------------------------
# Multi-path rtunnel_bin (colon-separated list, $PATH-style)
#
# Users who work across multiple storage partitions (hdd / ssd / qb-ilm) keep
# a rtunnel copy in each and configure them all; bootstrap should try each in
# order and use the first that actually exists on the container.
# ---------------------------------------------------------------------------


def test_rtunnel_bin_colon_string_emits_guarded_copies_per_path() -> None:
    runtime = SshRuntimeConfig(
        rtunnel_bin="/inspire/hdd/rtunnel:/inspire/ssd/rtunnel:/inspire/qb-ilm/rtunnel",
    )
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    # First path stays as RTUNNEL_BIN_PATH for legacy `cp "$RTUNNEL_BIN_PATH"` shell.
    assert "RTUNNEL_BIN_PATH=/inspire/hdd/rtunnel" in joined
    assert 'cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel' in joined

    # Additional paths become their own guarded copy lines.
    assert "cp /inspire/ssd/rtunnel /tmp/rtunnel" in joined
    assert "cp /inspire/qb-ilm/rtunnel /tmp/rtunnel" in joined

    # The extra-path copies must be first-match-wins: only run if /tmp/rtunnel
    # hasn't been populated by an earlier candidate.
    for extra in ("/inspire/ssd/rtunnel", "/inspire/qb-ilm/rtunnel"):
        assert f"[ ! -x /tmp/rtunnel ] && [ -f {extra} ]" in joined


def test_rtunnel_bin_list_input_accepted_directly() -> None:
    runtime = SshRuntimeConfig(
        rtunnel_bin=["/a/rtunnel", "/b/rtunnel"],  # type: ignore[arg-type]
    )
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    assert "RTUNNEL_BIN_PATH=/a/rtunnel" in joined
    assert "cp /b/rtunnel /tmp/rtunnel" in joined


def test_rtunnel_bin_multi_path_order_preserved() -> None:
    runtime = SshRuntimeConfig(rtunnel_bin="/first/rtunnel:/second/rtunnel:/third/rtunnel")
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )

    primary_idx = None
    second_idx = None
    third_idx = None
    for i, line in enumerate(commands):
        if primary_idx is None and 'cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel' in line:
            primary_idx = i
        if second_idx is None and "cp /second/rtunnel /tmp/rtunnel" in line:
            second_idx = i
        if third_idx is None and "cp /third/rtunnel /tmp/rtunnel" in line:
            third_idx = i

    assert primary_idx is not None
    assert second_idx is not None
    assert third_idx is not None
    assert primary_idx < second_idx < third_idx


def test_rtunnel_bin_empty_emits_empty_placeholder() -> None:
    runtime = SshRuntimeConfig(rtunnel_bin=None)
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )

    # RTUNNEL_BIN_PATH placeholder must still be set so downstream
    # openssh_bootstrap_cmd / ensure_rtunnel_cmd can reference it safely.
    assert any(line == "RTUNNEL_BIN_PATH=''" for line in commands), (
        "Expected a top-level `RTUNNEL_BIN_PATH=''` line, got: "
        + repr([c for c in commands if "RTUNNEL_BIN_PATH" in c])
    )
    # And no top-level unconditional copy line (the bootstrap block's internal
    # copy is gated by its own `[ -n "${RTUNNEL_BIN_PATH:-}" ]` guard so an
    # empty RTUNNEL_BIN_PATH never runs it at shell time).
    top_level_copy = (
        'if [ -f "$RTUNNEL_BIN_PATH" ]; then cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel '
        "&& chmod +x /tmp/rtunnel; fi"
    )
    assert top_level_copy not in commands
