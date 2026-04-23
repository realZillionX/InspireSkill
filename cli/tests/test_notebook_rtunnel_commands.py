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
# contents_api_filename parameter
# ---------------------------------------------------------------------------


def test_contents_api_filename_inserts_copy_search_command() -> None:
    runtime = SshRuntimeConfig()
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
        contents_api_filename=".inspire_rtunnel_bin",
    )
    joined = "\n".join(commands)

    assert ".inspire_rtunnel_bin" in joined
    assert "CONTENTS_API_RTUNNEL_FILE=" in joined
    assert '"$PWD/$CONTENTS_API_RTUNNEL_FILE"' in joined
    assert '"$HOME/$CONTENTS_API_RTUNNEL_FILE"' in joined
    assert 'cp "$_rtunnel_candidate" /tmp/rtunnel' in joined
    assert "chmod +x /tmp/rtunnel" in joined
    assert "[ ! -x /tmp/rtunnel ]" in joined


def test_contents_api_filename_none_has_no_move_command() -> None:
    runtime = SshRuntimeConfig()
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
        contents_api_filename=None,
    )
    joined = "\n".join(commands)

    assert ".inspire_rtunnel_bin" not in joined


def test_contents_api_filename_does_not_override_rtunnel_bin_path() -> None:
    runtime = SshRuntimeConfig(
        rtunnel_bin="/project/rtunnel",
    )
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
        contents_api_filename=".inspire_rtunnel_bin",
    )

    # Find first indices of the RTUNNEL_BIN_PATH copy and the contents API copy loop
    bin_path_idx = None
    contents_api_idx = None
    for i, line in enumerate(commands):
        if bin_path_idx is None and 'cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel' in line:
            bin_path_idx = i
        if contents_api_idx is None and "CONTENTS_API_RTUNNEL_FILE=" in line:
            contents_api_idx = i

    assert bin_path_idx is not None, "RTUNNEL_BIN_PATH copy line not found"
    assert contents_api_idx is not None, "Contents API copy block not found"
    assert (
        bin_path_idx < contents_api_idx
    ), "RTUNNEL_BIN_PATH copy must come before contents API copy block"


# ---------------------------------------------------------------------------
# Container-preinstalled rtunnel probe (unified-base:v1 and similar images
# bake rtunnel into /usr/local/bin, so bootstrap must prefer it over any
# Contents API upload — the latter writes to the Jupyter root_dir, which is
# the user's project-fileset and returns HTTP 500 / Errno 122 when quota-full).
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


def test_preinstalled_probe_sits_between_bin_path_and_contents_api() -> None:
    runtime = SshRuntimeConfig(rtunnel_bin="/project/rtunnel")
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
        contents_api_filename=".inspire_rtunnel_bin",
    )

    bin_path_idx = None
    probe_idx = None
    contents_api_idx = None
    for i, line in enumerate(commands):
        if bin_path_idx is None and 'cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel' in line:
            bin_path_idx = i
        if probe_idx is None and "command -v rtunnel" in line:
            probe_idx = i
        if contents_api_idx is None and "CONTENTS_API_RTUNNEL_FILE=" in line:
            contents_api_idx = i

    assert bin_path_idx is not None
    assert probe_idx is not None
    assert contents_api_idx is not None
    assert bin_path_idx < probe_idx < contents_api_idx, (
        "Order must be: explicit RTUNNEL_BIN_PATH override → PATH probe "
        "→ Contents API fallback"
    )
