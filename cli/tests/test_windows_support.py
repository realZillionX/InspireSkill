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

import os
import subprocess
import sys
import time
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
from inspire.services.utils.processes import detached_creationflags, process_is_alive


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
        [
            r"C:\Users\me\.local\bin\inspire.exe",
            "notebook",
            "ssh-proxy",
            "%h",
            "--workspace",
            "弹性计算",
        ]
    )

    assert "'" not in command
    assert command == subprocess.list2cmdline(
        [
            r"C:\Users\me\.local\bin\inspire.exe",
            "notebook",
            "ssh-proxy",
            "%h",
            "--workspace",
            "弹性计算",
        ]
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


def test_liveness_probe_reports_this_process_and_not_a_dead_one() -> None:
    assert process_is_alive(os.getpid()) is True
    assert process_is_alive(0) is False
    # High enough to be unallocated on both platforms; the important contract
    # is that the probe returns without sending a signal.
    assert process_is_alive(2**31 - 1) is False


def test_liveness_probe_never_reaches_os_kill_on_windows(
    as_windows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On Windows signal.CTRL_C_EVENT is numeric zero, so this POSIX-looking
    # call interrupts the target's whole console process group.
    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("os.kill must not be used to probe liveness on Windows")

    monkeypatch.setattr(os, "kill", explode)

    # ctypes.windll is unavailable on a POSIX test host. Reaching that lookup
    # proves the Windows branch was selected without touching os.kill; the real
    # Windows CI job exercises the call successfully.
    import ctypes

    if hasattr(ctypes, "windll"):
        assert process_is_alive(os.getpid()) is True
    else:
        with pytest.raises(AttributeError):
            process_is_alive(os.getpid())


@pytest.mark.parametrize("failures", [0, 2, 5])
def test_atomic_replace_retries_windows_sharing_violation(
    as_windows, monkeypatch, tmp_path, failures
):
    from inspire import local_files

    target = tmp_path / "current"
    target.write_text("old")
    real_replace = os.replace
    attempts = []
    pauses = []

    def replace(source, destination):
        attempts.append(source)
        if len(attempts) <= failures:
            raise PermissionError("fixture sharing violation")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(local_files.time, "sleep", pauses.append)
    if failures == 5:
        with pytest.raises(PermissionError, match="sharing violation"):
            local_files.atomic_write_text(target, "new")
        assert target.read_text() == "old"
    else:
        local_files.atomic_write_text(target, "new")
        assert target.read_text() == "new"
    assert len(attempts) == min(failures + 1, 5)
    assert len(pauses) == min(failures, 4)
    assert sum(pauses) <= 0.5
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.usefixtures("windows_directory_acl")
def test_windows_private_writer_protects_directory_before_creating_temporary(
    as_windows, monkeypatch, tmp_path
):
    from pathlib import Path
    from inspire import local_files

    protected = []

    def protect(path, **kwargs):
        assert Path(path).is_dir()
        assert list(Path(path).iterdir()) == []
        protected.append(path)

    monkeypatch.setattr(local_files, "restrict_windows_file", protect)
    target = tmp_path / "private.json"
    local_files.atomic_write_text(target, "fixture payload", private=True)
    assert len(protected) == 1
    assert target.read_text() == "fixture payload"


@pytest.mark.parametrize("reason", ["missing PowerShell", "ACL verification failed"])
@pytest.mark.usefixtures("windows_directory_acl")
def test_windows_cache_acl_failure_is_reported_once_and_session_still_saves(
    as_windows, monkeypatch, tmp_path, caplog, reason
):
    from pathlib import Path
    from inspire import local_files
    from inspire.platform.web.session.models import WebSession

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fail(path, **kwargs):
        assert Path(path).is_dir()
        raise ValueError(reason)

    monkeypatch.setattr(local_files, "restrict_windows_file", fail)
    session = WebSession(
        storage_state={"cookies": [], "origins": []}, account="fixture", created_at=time.time()
    )
    session.save()
    session.save()
    path = tmp_path / ".inspire/accounts/fixture/web_session.json"
    records = [record for record in caplog.records if str(path.parent) in record.getMessage()]
    assert len(records) == 1
    assert reason in records[0].getMessage()
    assert WebSession.load(account="fixture") is not None


@pytest.mark.parametrize("failure", ["missing", "timeout", "denied", "verification"])
def test_windows_acl_errors_are_safe_and_actionable(as_windows, monkeypatch, tmp_path, failure):
    from inspire import local_files

    monkeypatch.setattr(
        local_files.shutil, "which", lambda name: None if failure == "missing" else name
    )

    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("fixture", 30)
        if failure == "denied":
            raise PermissionError("fixture denied")
        return subprocess.CompletedProcess([], 1, b"", b"untrusted subprocess diagnostic")

    monkeypatch.setattr(local_files.subprocess, "run", run)
    with pytest.raises(ValueError) as caught:
        local_files.restrict_windows_file(str(tmp_path / "empty"))
    assert "PowerShell" in str(caught.value) or "private Windows file permissions" in str(
        caught.value
    )
    assert "untrusted subprocess diagnostic" not in str(caught.value)


@pytest.mark.usefixtures("windows_directory_acl")
def test_windows_directory_repair_preserves_owner_access_and_enables_inheritance(as_windows, monkeypatch, tmp_path):
    from inspire import local_files

    target = tmp_path / "old"
    target.touch()
    calls = []
    monkeypatch.setattr(local_files, "restrict_windows_file", lambda *a, **k: calls.append((a, k)))
    local_files.restrict_private_path(target)
    assert calls == [((str(target.parent),), {"repair": True})]
    repair_script = local_files._WINDOWS_ACL_SCRIPT.split("if ($env:INSPIRE_PRIVATE_REPAIR", 1)[
        1
    ].split("$acl = if", 1)[0]
    assert "RemoveAccessRuleSpecific" in repair_script
    assert "ContainerInherit,ObjectInherit" in repair_script
    assert "Private directory inheritance verification failed" in repair_script
    assert "SetOwner" not in repair_script
    assert "Get-Acl" in repair_script


@pytest.mark.parametrize("operation", ["exec", "transfer", "stream"])
def test_async_subprocess_requires_proactor_without_changing_policy(
    as_windows, monkeypatch, operation
):
    import asyncio
    from inspire.bridge.tunnel import process_async
    from inspire.platform.errors import ConfigurationError, InspireError

    async def unsupported(*args, **kwargs):
        raise NotImplementedError

    def forbidden(*args, **kwargs):
        raise AssertionError("SDK must not change the caller's loop policy")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unsupported)
    policy = asyncio.get_event_loop_policy()
    monkeypatch.setattr(asyncio, "set_event_loop_policy", forbidden)

    async def exercise():
        with pytest.raises(InspireError, match="ProactorEventLoop") as caught:
            if operation == "stream":
                await process_async.stream_process(
                    ["ssh"],
                    None,
                    lambda text: None,
                    None,
                    None,
                    None,
                    "fixture",
                    deliver=lambda *args: None,
                )
            else:
                await process_async.run_process(["scp" if operation == "transfer" else "ssh"])
        assert isinstance(caught.value, ConfigurationError)
        assert isinstance(caught.value.__cause__, NotImplementedError)
        assert "before starting" in str(caught.value)

    asyncio.run(exercise())
    assert asyncio.get_event_loop_policy() is policy


@pytest.mark.parametrize("kind", ["guard", "ide", "rtunnel", "catalog", "index", "bridges"])
@pytest.mark.usefixtures("windows_directory_acl")
def test_windows_private_cache_paths_reach_shared_acl(as_windows, monkeypatch, tmp_path, kind):
    from pathlib import Path
    from inspire import local_files

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    directories = []

    def protect(path, **kwargs):
        candidate = Path(path)
        assert candidate.is_dir()
        directories.append(candidate)
        # The parent is protected before any cache file is created.
        assert not list(candidate.iterdir())

    monkeypatch.setattr(local_files, "restrict_windows_file", protect)
    path = tmp_path / "fixture.json"
    if kind == "guard":
        from inspire.platform.web.session.login_guard import _LoginBlock, _store

        _store(path, _LoginBlock(1, 2, 1, "fixture-derived", None, False))
    elif kind == "ide":
        from inspire.platform.web.browser_api.playwright_notebooks import _save_ide_url_cache

        _save_ide_url_cache(path, {"notebooks": {}})
    elif kind == "rtunnel":
        from inspire.platform.web.browser_api.rtunnel import _save_state_file

        _save_state_file(path, {"notebooks": {}})
    elif kind == "catalog":
        from inspire.sdk.catalog_store import CatalogStore

        # This test exercises the writer with an arbitrary destination; account
        # resolution and its three directories have a separate lock-order test.
        monkeypatch.setattr("inspire.sdk.catalog_store.account_dir", lambda name, **kwargs: tmp_path)
        store = CatalogStore("fixture", "https://example.invalid")
        store.path = path
        store._write({"entries": {}})
    elif kind == "index":
        from inspire.services.catalog.resource_index import ResourceIndex

        ResourceIndex(path)
    else:
        from inspire.bridge.tunnel.config import save_tunnel_config

        config = TunnelConfig(config_dir=tmp_path)
        path = config.config_file
        save_tunnel_config(config)
    assert directories == [path.parent]
    assert path.stat().st_size > 0


@pytest.mark.usefixtures("windows_directory_acl")
def test_windows_unrepairable_acl_is_left_unchanged_with_a_reason(
    as_windows, monkeypatch, tmp_path, caplog
):
    from inspire import local_files

    target = tmp_path / "old"
    target.touch()
    monkeypatch.setattr(local_files.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        local_files.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 3),
    )
    local_files.restrict_private_path(target)
    assert "left unchanged to avoid removing your access" in caplog.text
    assert str(target.parent) in caplog.text


@pytest.mark.parametrize("operations", [1, 10, 100])
@pytest.mark.usefixtures("windows_directory_acl")
def test_windows_private_io_acl_cost_is_bounded(as_windows, monkeypatch, tmp_path, operations):
    """Count real ACL launch requests, not calls to a mocked permission helper."""
    from collections import Counter
    from pathlib import Path
    from inspire import local_files
    from inspire.config.toml import _load_toml

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(local_files.shutil, "which", lambda name: name)
    calls = []

    def run(args, **kwargs):
        assert args[0] == "powershell.exe"
        directory = Path(kwargs["env"]["INSPIRE_KEY_EXPORT_PATH"])
        assert directory.is_dir()
        calls.append(directory)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(local_files.subprocess, "run", run)
    root = tmp_path / ".inspire"
    target = root / "accounts/fixture/config.toml"
    for _ in range(operations):
        local_files.atomic_write_text(target, 'title = "fixture"\n', private=True)
        assert _load_toml(target) == {"title": "fixture"}
        local_files.restrict_private_path(target)
        local_files.restrict_private_path(target.parent)
    assert Counter(calls) == {root: 1, root / "accounts": 1, target.parent: 1}
    # A second account needs just its own directory; the shared parents are cached.
    other = root / "accounts/other/config.toml"
    local_files.atomic_write_text(other, 'title = "fixture"\n', private=True)
    _load_toml(other)
    assert calls[-1] == other.parent
    assert len(calls) == 4


@pytest.mark.parametrize("returncode", [1, 3])
@pytest.mark.usefixtures("windows_directory_acl")
def test_windows_directory_acl_failure_is_not_retried_per_operation(
    as_windows, monkeypatch, tmp_path, caplog, returncode
):
    from inspire import local_files

    monkeypatch.setattr(local_files.shutil, "which", lambda name: name)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode)

    monkeypatch.setattr(local_files.subprocess, "run", run)
    for _ in range(10):
        local_files.atomic_write_text(tmp_path / "fixture.toml", "", private=True)
    assert len(calls) == 1
    assert len(caplog.records) == 1


@pytest.mark.usefixtures("windows_directory_acl")
def test_windows_missing_inspire_home_does_not_restrict_parent(as_windows, monkeypatch, tmp_path):
    from pathlib import Path
    from inspire import local_files

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("A missing Inspire path must not change the user's home ACL")

    monkeypatch.setattr(local_files.subprocess, "run", forbidden)
    monkeypatch.setattr(local_files.shutil, "which", lambda name: name)
    local_files.repair_inspire_path(tmp_path / ".inspire/config.toml")


@pytest.mark.usefixtures("windows_directory_acl")
def test_windows_concurrent_directory_initialization_launches_once(
    as_windows, monkeypatch, tmp_path
):
    from concurrent.futures import ThreadPoolExecutor
    from inspire import local_files

    monkeypatch.setattr(local_files.shutil, "which", lambda name: name)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        time.sleep(0.01)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(local_files.subprocess, "run", run)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(local_files.restrict_private_path, [tmp_path] * 16))
    assert len(calls) == 1


@pytest.mark.parametrize("returncode", [0, 1])
@pytest.mark.usefixtures("windows_directory_acl")
def test_windows_export_still_verifies_file_in_cached_directory(
    as_windows, monkeypatch, tmp_path, returncode
):
    from pathlib import Path
    from inspire import local_files
    from inspire.cli.commands.account.key_export import export_private_key

    monkeypatch.setattr(local_files.shutil, "which", lambda name: name)
    calls = []

    def run(args, **kwargs):
        path = Path(kwargs["env"]["INSPIRE_KEY_EXPORT_PATH"])
        calls.append(path)
        if path.is_dir():
            return subprocess.CompletedProcess(args, 0)
        assert path.read_bytes() == b""
        assert kwargs["env"]["INSPIRE_PRIVATE_REPAIR"] == "0"
        return subprocess.CompletedProcess(args, returncode)

    monkeypatch.setattr(local_files.subprocess, "run", run)
    local_files.ensure_private_directory(tmp_path)
    output = tmp_path / "export.txt"
    if returncode:
        with pytest.raises(ValueError, match="Could not verify"):
            export_private_key("fixture payload", output)
        assert not output.exists()
    else:
        export_private_key("fixture payload", output)
        assert output.read_text() == "fixture payload"
    assert len(calls) == 2
    assert not list(tmp_path.glob(".inspire-key-*"))


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("operations", [1, 16])
@pytest.mark.parametrize("returncode", [0, 1])
@pytest.mark.usefixtures("windows_directory_acl")
def test_windows_catalog_acl_is_warm_before_cache_lock(
    as_windows, monkeypatch, tmp_path, existing, operations, returncode
):
    from collections import Counter
    from pathlib import Path
    from inspire import local_files
    from inspire.accounts import cache_lock
    from inspire.sdk.catalog_store import CatalogStore

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".inspire"
    directory = root / "accounts/fixture"
    if existing:
        directory.mkdir(parents=True)
    held = 0
    acquisitions = 0
    acquire = cache_lock._acquire
    release = cache_lock._release

    def tracked_acquire(*args, **kwargs):
        nonlocal held, acquisitions
        assert kwargs["timeout"] == 5
        acquire(*args, **kwargs)
        held += 1
        acquisitions += 1

    def tracked_release(*args, **kwargs):
        nonlocal held
        release(*args, **kwargs)
        held -= 1

    calls = []

    def run(args, **kwargs):
        assert held == 0, "ACL subprocess launched while holding a cache lock"
        assert args[0] == "powershell.exe"
        calls.append(Path(kwargs["env"]["INSPIRE_KEY_EXPORT_PATH"]))
        return subprocess.CompletedProcess(args, returncode)

    monkeypatch.setattr(cache_lock, "_acquire", tracked_acquire)
    monkeypatch.setattr(cache_lock, "_release", tracked_release)
    monkeypatch.setattr(local_files.shutil, "which", lambda name: name)
    monkeypatch.setattr(local_files.subprocess, "run", run)
    for i in range(operations):
        # Re-resolving an account and constructing a new store cannot reset the memo.
        store = CatalogStore("fixture", "https://example.invalid")
        key = ("current_user", "fixture", store.base_url, str(i))
        generation, _ = store.read(key)
        entry = store.put(key, {"id": str(i)}, 60, generation)
        assert entry is not None and store.value(entry) == {"id": str(i)}
    store.invalidate()
    assert acquisitions == 2 * operations + 1
    assert held == 0
    assert Counter(calls) == {root: 1, root / "accounts": 1, directory: 1}


@pytest.mark.usefixtures("windows_directory_acl")
def test_windows_account_lookup_does_not_create_missing_directories(
    as_windows, monkeypatch, tmp_path
):
    from pathlib import Path
    from inspire import local_files
    from inspire.accounts.storage import account_dir

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("A missing account lookup must not launch PowerShell")

    monkeypatch.setattr(local_files.shutil, "which", lambda name: name)
    monkeypatch.setattr(local_files.subprocess, "run", forbidden)
    assert account_dir("fixture") == tmp_path / ".inspire/accounts/fixture"
    assert not (tmp_path / ".inspire").exists()
