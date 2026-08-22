"""Windows-native behaviour that cannot be exercised from a POSIX test host.

Each test pins a decision that depends on a Windows API contract rather than on
Python, so the reasoning lives next to the assertion:

- Win32-OpenSSH runs ProxyCommand through ``posix_spawnp`` → ``CreateProcessW``
  with no shell (``FORK_NOT_SUPPORTED`` in ``config.h.vs``), so redirection and
  POSIX quoting in that string are passed through to the child as arguments.
- Its ``open()`` maps both ``/dev/null`` and ``NUL`` to the Windows null device
  (``NULL_DEVICE`` / ``NULL_DEVICE_WIN`` in ``misc_internal.h``), so the POSIX
  spelling stays correct on every platform.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import PureWindowsPath

import pytest

from inspire.bridge.tunnel.models import BridgeProfile, TunnelConfig
from inspire.bridge.tunnel.scp import _build_scp_base_args
from inspire.bridge.tunnel.ssh import _get_proxy_command, build_ssh_process_env
from inspire.bridge.tunnel.ssh_exec import _build_ssh_base_args
from inspire.cli.commands.notebook.ssh_config_cmd import (
    _quote_proxy_command,
    _quote_ssh_config_value,
)
from inspire.cli.commands.uninstall import _playwright_cache_dir
from inspire.cli.console_bootstrap import configure_console_encoding
from inspire.cli.utils.detached import detached_creationflags


@pytest.fixture
def bridge() -> BridgeProfile:
    return BridgeProfile(name="demo", proxy_url="https://example.invalid/proxy/31337/?token=a?b")


@pytest.fixture
def as_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Present a win32 platform to every module that branches on it."""
    monkeypatch.setattr("sys.platform", "win32")


def test_rtunnel_lands_next_to_the_cli_with_an_exe_suffix(as_windows: None) -> None:
    # ~/.local/bin is not on PATH on Windows and a suffix-less file is not
    # executable there, so the binary needs its own home and a real extension.
    assert TunnelConfig().rtunnel_bin.name == "rtunnel.exe"


def test_ssh_helpers_keep_the_posix_null_device_spelling(
    as_windows: None, bridge: BridgeProfile
) -> None:
    assert "UserKnownHostsFile=/dev/null" in _build_ssh_base_args(bridge=bridge, proxy_cmd="p")
    assert "UserKnownHostsFile=/dev/null" in _build_scp_base_args(bridge=bridge, proxy_cmd="p")


def test_windows_proxy_command_carries_no_shell_syntax(
    as_windows: None, bridge: BridgeProfile
) -> None:
    # `2>NUL` here would reach rtunnel as a fourth positional argument, which it
    # rejects with "invalid number of arguments" — so `quiet` cannot redirect.
    quiet = _get_proxy_command(bridge, PureWindowsPath(r"C:\i\bin\rtunnel.exe"), quiet=True)
    loud = _get_proxy_command(bridge, PureWindowsPath(r"C:\i\bin\rtunnel.exe"), quiet=False)

    assert quiet == loud
    for shell_syntax in ("2>", "sh -c", "|", "&&"):
        assert shell_syntax not in quiet


def test_windows_proxy_command_quotes_every_token(as_windows: None, bridge: BridgeProfile) -> None:
    # A leading double quote is what makes OpenSSH's build_commandline_string()
    # forward the string unmodified instead of applying its .exe heuristic.
    command = _get_proxy_command(bridge, PureWindowsPath(r"C:\Program Files\i\rtunnel.exe"))

    assert command.startswith('"C:\\Program Files\\i\\rtunnel.exe"')
    assert command.endswith('"stdio://%h:%p"')


def test_windows_ssh_env_carries_the_proxy_override(
    as_windows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No shell means no `VAR=value` prefix, so the override has to reach rtunnel
    # through the environment ssh itself was started with.
    monkeypatch.setattr(
        "inspire.bridge.tunnel.ssh.get_rtunnel_proxy_override",
        lambda: "http://proxy.invalid:8080",
    )

    env = build_ssh_process_env()

    assert env["HTTPS_PROXY"] == "http://proxy.invalid:8080"
    assert env["LC_ALL"] == "C"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX puts it in the ProxyCommand")
def test_posix_ssh_env_leaves_the_proxy_to_the_proxy_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "inspire.bridge.tunnel.ssh.get_rtunnel_proxy_override",
        lambda: "http://proxy.invalid:8080",
    )

    # Compared against the override rather than asserted absent: a developer
    # machine may legitimately have HTTPS_PROXY set already.
    assert build_ssh_process_env().get("HTTPS_PROXY") != "http://proxy.invalid:8080"


def test_ssh_config_proxy_command_uses_windows_quoting(as_windows: None) -> None:
    # shlex.quote wraps every non-ASCII token in single quotes, and Chinese
    # workspace names make that the norm rather than the exception.
    command = _quote_proxy_command(
        [r"C:\Users\me\.local\bin\inspire.exe", "notebook", "ssh-proxy", "%h", "--workspace", "弹性计算"]
    )

    assert "'" not in command
    assert command == subprocess.list2cmdline(
        [r"C:\Users\me\.local\bin\inspire.exe", "notebook", "ssh-proxy", "%h", "--workspace", "弹性计算"]
    )


def test_ssh_config_proxy_command_quotes_a_path_with_spaces(as_windows: None) -> None:
    command = _quote_proxy_command([r"C:\Program Files\i\inspire.exe", "notebook", "ssh-proxy"])

    assert command.startswith('"C:\\Program Files\\i\\inspire.exe"')


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("~/.ssh/id_ed25519", "~/.ssh/id_ed25519"),
        (r"C:\Users\me\.ssh\id_ed25519", r"C:\Users\me\.ssh\id_ed25519"),
        (r"C:\Users\First Last\.ssh\id_ed25519", r'"C:\Users\First Last\.ssh\id_ed25519"'),
    ],
)
def test_ssh_config_values_use_openssh_quoting(value: str, expected: str) -> None:
    # readconf.c's strdelim only recognises `"`, so single quotes would end up
    # inside the filename on every platform, not just Windows.
    assert _quote_ssh_config_value(value) == expected


def test_playwright_cache_lives_under_local_app_data(
    as_windows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\me\AppData\Local")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    cache_dir = _playwright_cache_dir()

    assert cache_dir is not None
    assert cache_dir.name == "ms-playwright"
    assert "AppData" in str(cache_dir)


class _RecordingStream:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def reconfigure(self, **kwargs: str) -> None:
        self.calls.append(kwargs)


def test_console_encoding_forces_utf8_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    stream = _RecordingStream()

    configure_console_encoding((stream,))

    assert stream.calls == [{"encoding": "utf-8", "errors": "replace"}]


@pytest.mark.skipif(sys.platform == "win32", reason="the no-op branch is the POSIX one")
def test_console_encoding_leaves_posix_streams_alone() -> None:
    stream = _RecordingStream()

    configure_console_encoding((stream,))

    assert stream.calls == []


def test_console_encoding_survives_a_stream_that_cannot_be_retuned(
    as_windows: None,
) -> None:
    class Detached:
        def reconfigure(self, **kwargs: str) -> None:
            raise ValueError("underlying buffer has been detached")

    class Plain:
        pass

    # Neither should take down the command that was about to print something.
    configure_console_encoding((Detached(), Plain()))


# Faking sys.platform is not enough here: the constants themselves only exist in
# the Windows stdlib, so each half runs where its assertion means something.
@pytest.mark.skipif(sys.platform != "win32", reason="the flags only exist on Windows")
def test_background_spawns_are_detached_from_the_console_on_windows() -> None:
    # start_new_session is accepted and ignored on Windows, which leaves the
    # update-check child sharing the parent's console — and therefore its
    # Ctrl-C. The detached flags keep it out of that console.
    expected = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]

    assert detached_creationflags() == expected


@pytest.mark.skipif(sys.platform == "win32", reason="Popen rejects non-zero flags on POSIX")
def test_background_spawns_pass_no_creationflags_off_windows() -> None:
    assert detached_creationflags() == 0
