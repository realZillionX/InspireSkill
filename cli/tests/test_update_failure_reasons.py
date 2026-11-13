"""Offline regression tests for update failure diagnostics."""

from __future__ import annotations

import http.client
import importlib
import io
import json
import subprocess
import tarfile
import urllib.error
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from conftest import set_fake_home
from inspire.cli.main import main

update = importlib.import_module("inspire.cli.commands.update")
audit_global_cli = update._audit_global_cli


@pytest.fixture(autouse=True)
def isolated_update(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_fake_home(monkeypatch, tmp_path)
    monkeypatch.delenv(update._FAILURE_MARKER_ENV, raising=False)
    monkeypatch.setattr(update, "HARNESS_ROOTS", {"antigravity": tmp_path})
    monkeypatch.setattr(update, "HARNESS_SKILL_DIRS", {"antigravity": tmp_path / "skill"})
    monkeypatch.setattr(update, "run_check", lambda **kw: {"current": "1.0", "latest": "1.0"})
    monkeypatch.setattr(update, "_audit_global_cli", lambda *a: (True, "1.0"))
    monkeypatch.setattr(update, "_upgrade_cli", lambda *a, **kw: True)
    monkeypatch.setattr(update, "_global_inspire_executable", lambda: "inspire")

    def no_network(*args: object, **kwargs: object) -> None:
        pytest.fail("Unexpected network request")

    monkeypatch.setattr(update.urllib.request, "urlopen", no_network)


def bundle(entries: dict[str, bytes]) -> bytes:
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for name, content in entries.items():
            info = tarfile.TarInfo("bundle/" + name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return result.getvalue()


@pytest.mark.parametrize("succeed", [True, False])
def test_download_retry_refresh(monkeypatch: pytest.MonkeyPatch, succeed: bool) -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def opener(request: object, *, timeout: int) -> io.BytesIO:
        calls.append(timeout)
        if len(calls) == 1 or not succeed:
            raise urllib.error.URLError("token=private /private/home")
        return io.BytesIO(bundle({"SKILL.md": b"skill"}))

    monkeypatch.setattr(update.urllib.request, "urlopen", opener)
    monkeypatch.setattr(update.time, "sleep", sleeps.append)
    with click.Context(update.update) as ctx:
        assert update._refresh_skill_files(True) is succeed
        reasons = ctx.meta.get("update_audit_failures", [])
    assert calls == [30, 30]
    assert sleeps == [2]
    assert reasons == (
        [] if succeed else ["Could not download the skill bundle from GitHub (URLError)."]
    )


@pytest.mark.parametrize(
    "json_output,silent", [(False, False), (True, False), (False, True), (True, True)]
)
def test_download_failure_output(
    monkeypatch: pytest.MonkeyPatch, json_output: bool, silent: bool
) -> None:
    def opener(*a: object, **kw: object) -> None:
        raise urllib.error.URLError("token=private /private/home")

    monkeypatch.setattr(update.urllib.request, "urlopen", opener)
    monkeypatch.setattr(update.time, "sleep", lambda _: None)
    args = (["--json"] if json_output else []) + ["update"] + (["--silent"] if silent else [])
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 1, result.output
    if silent:
        assert result.output == ""
    else:
        assert result.output.index("Could not download") < result.output.index(
            "SKILL.md is missing"
        )
        assert "private" not in result.output
        if json_output:
            assert "Could not download" in json.loads(result.output)["error"]["message"]


@pytest.mark.parametrize("json_output", [False, True])
def test_check_reports_missing_skill_without_downloading(json_output: bool) -> None:
    result = CliRunner().invoke(main, (["--json"] if json_output else []) + ["update", "--check"])
    assert result.exit_code == 1
    assert "SKILL.md is missing for antigravity" in result.output


@pytest.mark.parametrize(
    "payload,reason",
    [
        (
            json.dumps(
                [
                    "Could not download the skill bundle from GitHub (URLError).",
                    "SKILL.md is missing for antigravity.",
                ]
            ),
            "Could not download",
        ),
        ("not-json", "post-update setup exited with code 9"),
        ('{"reason": "ignored"}', "post-update setup exited with code 9"),
        ("[1]", "post-update setup exited with code 9"),
        ("[]", "post-update setup exited with code 9"),
        (None, "post-update setup exited with code 9"),
    ],
)
@pytest.mark.parametrize(
    "json_output,silent", [(False, False), (True, False), (False, True), (True, True)]
)
def test_self_upgrade_propagates_only_marker(
    monkeypatch: pytest.MonkeyPatch,
    payload: str | None,
    reason: str,
    json_output: bool,
    silent: bool,
) -> None:
    monkeypatch.setattr(update, "run_check", lambda **kw: {"current": "1.0", "latest": "999.0"})
    stderr = "raw child output /private/home token=private\n"
    if payload is not None:
        stderr += update._POST_UPDATE_FAILURE_MARKER + payload + "\n"
    monkeypatch.setattr(
        update.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 9, "raw stdout", stderr),
    )
    args = (["--json"] if json_output else []) + ["update"] + (["--silent"] if silent else [])
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 1, result.output
    if silent:
        assert result.output == ""
    else:
        assert reason in result.output
        assert "private" not in result.output
        assert "raw" not in result.output
        assert update._POST_UPDATE_FAILURE_MARKER not in result.output
        if json_output:
            assert reason in json.loads(result.output)["error"]["message"]


@pytest.mark.parametrize("silent", [False, True])
@pytest.mark.parametrize("hook", ["post", "runtime"])
@pytest.mark.parametrize("marker_env", [None, "0", "1"])
def test_child_hook_failure_output(
    monkeypatch: pytest.MonkeyPatch, silent: bool, hook: str, marker_env: str | None
) -> None:
    if marker_env is not None:
        monkeypatch.setenv(update._FAILURE_MARKER_ENV, marker_env)

    def fail(**kw: object) -> bool:
        update._record_audit_failure("Failed /private/home token=private")
        update._record_audit_failure("Another failure.")
        return False

    monkeypatch.setattr(
        update, "_run_post_update_tasks" if hook == "post" else "_ensure_playwright_runtime", fail
    )
    args = (
        ["_post-update", "--expected-version", "1.0"]
        if hook == "post"
        else ["_ensure-playwright-runtime"]
    )
    result = CliRunner().invoke(main, args + (["--silent"] if silent else []))
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "private" not in result.output
    reasons = ["Failed <path> token=<redacted>", "Another failure."]
    if marker_env == "1":
        assert result.stderr.startswith(update._POST_UPDATE_FAILURE_MARKER)
        assert json.loads(result.stderr.split("=", 1)[1]) == reasons
    else:
        assert update._POST_UPDATE_FAILURE_MARKER not in result.output
        assert result.stderr == ("" if silent else "".join(f"Error: {r}\n" for r in reasons))


@pytest.mark.parametrize("silent", [False, True])
@pytest.mark.parametrize("hook", ["post", "runtime"])
def test_parents_request_failure_marker(
    monkeypatch: pytest.MonkeyPatch, silent: bool, hook: str
) -> None:
    monkeypatch.setenv(update._FAILURE_MARKER_ENV, "0")
    environments: list[dict[str, str]] = []

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        environments.append(env)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(update.subprocess, "run", run)
    if hook == "post":
        assert update._run_post_update_command(
            expected_version="1.0", cli_only=False, silent=silent
        )
    else:
        assert update._ensure_global_playwright_runtime(silent)
    assert len(environments) == 1
    assert environments[0]["INSPIRE_FAILURE_MARKER"] == "1"
    assert environments[0]["INSPIRE_SKIP_UPDATE_CHECK"] == "1"
    assert update.os.environ[update._FAILURE_MARKER_ENV] == "0"


@pytest.mark.parametrize(
    "data,reason",
    [
        (b"invalid", "Skill bundle is invalid"),
        (bundle({"README.md": b"readme"}), "SKILL.md is missing from the skill bundle"),
    ],
)
def test_invalid_bundle(monkeypatch: pytest.MonkeyPatch, data: bytes, reason: str) -> None:
    monkeypatch.setattr(update, "_download_tarball", lambda: data)
    with click.Context(update.update) as ctx:
        assert not update._refresh_skill_files(True)
        assert reason in ctx.meta["update_audit_failures"][0]


@pytest.mark.parametrize("failure", ["clean", "copy", "verify", "temp"])
def test_filesystem_failure_reasons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    monkeypatch.setattr(update, "_download_tarball", lambda: bundle({"SKILL.md": b"skill"}))

    def fail(*a: object, **kw: object) -> None:
        raise PermissionError("/private/home token=private")

    if failure == "clean":
        (tmp_path / "skill").write_text("old")
        monkeypatch.setattr(Path, "unlink", fail)
    elif failure == "copy":
        monkeypatch.setattr(update.shutil, "copy2", fail)
    elif failure == "temp":
        monkeypatch.setattr(update.tempfile, "TemporaryDirectory", fail)
    else:
        monkeypatch.setattr(update, "_verify_skill_target", lambda *a: ["/private/home"])
    with click.Context(update.update) as ctx:
        assert not update._refresh_skill_files(True)
        reasons = ctx.meta["update_audit_failures"]
    assert reasons
    assert "private" not in str(reasons)


@pytest.mark.parametrize("failure", ["missing", "start", "exit"])
def test_runtime_failure_reason(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    def run(*a: object, **kw: object) -> subprocess.CompletedProcess[str]:
        if failure == "start":
            raise OSError("/private/home")
        return subprocess.CompletedProcess([], 7, "", "token=private")

    if failure == "missing":
        monkeypatch.setattr(update, "_global_inspire_executable", lambda: None)
    monkeypatch.setattr(update.subprocess, "run", run)
    with click.Context(update.update) as ctx:
        assert not update._ensure_global_playwright_runtime(True)
        assert "Browser runtime setup failed" in ctx.meta["update_audit_failures"][0]
        assert "private" not in str(ctx.meta)
        if failure == "exit":
            assert ctx.meta["update_audit_failures"] == [
                "Browser runtime setup failed: exited with code 7."
            ]


@pytest.mark.parametrize(
    "error,retries",
    [
        (TimeoutError(), 1),
        (ConnectionResetError(), 1),
        (urllib.error.HTTPError("https://example.test", 503, "unavailable", {}, None), 1),
        (urllib.error.HTTPError("https://example.test", 404, "missing", {}, None), 0),
        (PermissionError(), 0),
    ],
)
def test_download_retry_is_bounded_and_selective(
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
    retries: int,
) -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def opener(request: object, *, timeout: int) -> None:
        calls.append(timeout)
        raise error

    monkeypatch.setattr(update.urllib.request, "urlopen", opener)
    monkeypatch.setattr(update.time, "sleep", sleeps.append)
    with click.Context(update.update) as ctx:
        assert update._download_tarball(timeout=7) is None
        assert len(ctx.meta["update_audit_failures"]) == 1
    assert calls == [7] * (retries + 1)
    assert sleeps == [2] * retries


@pytest.mark.parametrize("check_only", [False, True])
@pytest.mark.parametrize("json_output", [False, True])
def test_recorded_download_reason_precedes_audit_in_failure_formatter(
    monkeypatch: pytest.MonkeyPatch,
    check_only: bool,
    json_output: bool,
) -> None:
    from inspire.cli.context import Context

    @click.command()
    def command() -> None:
        update._record_audit_failure("Could not download the skill bundle from GitHub (URLError).")
        assert not update._audit_installed_skills(True)
        update._emit_update_failure(silent=False, check_only=check_only)

    context = Context()
    context.json_output = json_output
    result = CliRunner().invoke(command, obj=context)
    assert result.exit_code == 0, result.output
    assert result.output.index("Could not download") < result.output.index("SKILL.md is missing")
    assert f"InspireSkill {'check' if check_only else 'update'} failed" in result.output


def test_truncated_bundle_records_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "_download_tarball", lambda: bundle({"SKILL.md": b"skill"})[:25])
    with click.Context(update.update) as ctx:
        assert not update._refresh_skill_files(True)
        assert "Skill bundle is invalid" in ctx.meta["update_audit_failures"][0]


@pytest.mark.parametrize(
    "raw",
    [
        r"C:\Users\sentinel\app.exe",
        r"C:/Users/sentinel/app.exe",
        r"\\server\share\sentinel\app.exe",
        "Authorization: Bearer sentinel",
        "authorization: Basic sentinel",
        "Bearer sentinel",
    ],
)
def test_scrub_windows_paths_and_credentials(raw: str) -> None:
    assert "sentinel" not in update._scrub_debug_text(raw)
    with click.Context(update.update) as ctx:
        proc = subprocess.CompletedProcess(
            [], 1, "", update._POST_UPDATE_FAILURE_MARKER + json.dumps([raw])
        )
        update._record_child_failures(proc, fallback="fallback")
        assert "sentinel" not in str(ctx.meta)


@pytest.mark.parametrize(
    "mode,summary",
    [
        ("missing", "not on PATH"),
        ("error", "could not run (OSError)"),
        ("exit", "exited with code 7"),
        ("invalid", "unrecognised version output"),
    ],
)
def test_version_probe_records_only_safe_summary(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    summary: str,
) -> None:
    raw = r"sentinel C:\Users\private\app \\server\share Authorization: Bearer secret"
    monkeypatch.setattr(update, "_uv_tool_info", lambda: None)
    monkeypatch.setattr(update.shutil, "which", lambda _: None if mode == "missing" else "inspire")

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if mode == "error":
            raise OSError(raw)
        return subprocess.CompletedProcess([], 7 if mode == "exit" else 0, raw, raw)

    monkeypatch.setattr(update.subprocess, "run", run)
    with click.Context(update.update) as ctx:
        assert audit_global_cli("1.0", True) == (False, None)
        reasons = ctx.meta["update_audit_failures"]
        assert summary in reasons[0]
        assert "sentinel" not in str(reasons)
        assert "secret" not in str(reasons)


@pytest.mark.parametrize("succeed", [False, True])
def test_incomplete_read_retries_once(monkeypatch: pytest.MonkeyPatch, succeed: bool) -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    class Response(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            if len(calls) == 1 or not succeed:
                raise http.client.IncompleteRead(b"sentinel", 100)
            return b"bundle"

    def opener(request: object, *, timeout: int) -> Response:
        calls.append(timeout)
        return Response()

    monkeypatch.setattr(update.urllib.request, "urlopen", opener)
    monkeypatch.setattr(update.time, "sleep", sleeps.append)
    with click.Context(update.update) as ctx:
        assert update._download_tarball() == (b"bundle" if succeed else None)
        assert ctx.meta.get("update_audit_failures", []) == (
            [] if succeed else ["Could not download the skill bundle from GitHub (IncompleteRead)."]
        )
    assert calls == [30, 30]
    assert sleeps == [2]


@pytest.mark.parametrize("silent", [False, True])
@pytest.mark.parametrize("hook", ["post", "runtime"])
def test_unexpected_child_exception_emits_marker(
    monkeypatch: pytest.MonkeyPatch,
    silent: bool,
    hook: str,
) -> None:
    monkeypatch.setenv(update._FAILURE_MARKER_ENV, "1")
    def crash(**kwargs: object) -> bool:
        raise RuntimeError("sentinel /private/home")

    monkeypatch.setattr(
        update, "_run_post_update_tasks" if hook == "post" else "_ensure_playwright_runtime", crash
    )
    args = (
        ["_post-update", "--expected-version", "1.0"]
        if hook == "post"
        else ["_ensure-playwright-runtime"]
    )
    result = CliRunner().invoke(main, args + (["--silent"] if silent else []))
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "sentinel" not in result.stderr
    assert "Traceback" not in result.stderr
    reasons = json.loads(result.stderr.removeprefix(update._POST_UPDATE_FAILURE_MARKER))
    assert len(reasons) == 1
    assert "crashed (RuntimeError)" in reasons[0]


@pytest.mark.parametrize("silent", [False, True])
@pytest.mark.parametrize("installed", [False, True])
def test_runtime_specific_reason_crosses_process_boundary(
    monkeypatch: pytest.MonkeyPatch,
    silent: bool,
    installed: bool,
) -> None:
    monkeypatch.setenv(update._FAILURE_MARKER_ENV, "1")
    monkeypatch.setattr(update, "_playwright_chromium_available", lambda: False)
    monkeypatch.setattr(update, "_install_playwright_chromium", lambda **kwargs: installed)
    result = CliRunner().invoke(
        main, ["_ensure-playwright-runtime"] + (["--silent"] if silent else [])
    )
    assert result.exit_code == 1
    reason = "Browser runtime setup failed: Chromium " + (
        "launch probe failed." if installed else "installation failed."
    )
    assert json.loads(result.stderr.removeprefix(update._POST_UPDATE_FAILURE_MARKER)) == [reason]

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE
        return subprocess.CompletedProcess([], 1, "", result.stderr)

    monkeypatch.setattr(update.subprocess, "run", run)
    with click.Context(update.update) as ctx:
        assert not update._ensure_global_playwright_runtime(silent)
        assert ctx.meta["update_audit_failures"] == [reason]


@pytest.mark.parametrize(
    "payload",
    [
        '["' + "x" * (64 * 1024) + '"]',
        "[" * 2000 + "]" * 2000,
        "[" + "9" * 5000 + "]",
    ],
)
@pytest.mark.parametrize("valid_after", [False, True])
def test_malformed_marker_is_bounded_and_scanning_continues(
    payload: str, valid_after: bool
) -> None:
    stderr = update._POST_UPDATE_FAILURE_MARKER + payload + "\n"
    if valid_after:
        stderr += update._POST_UPDATE_FAILURE_MARKER + '["safe reason"]'
    with click.Context(update.update) as ctx:
        update._record_child_failures(
            subprocess.CompletedProcess([], 9, "", stderr), fallback="exited with code 9"
        )
        assert ctx.meta["update_audit_failures"] == [
            "safe reason" if valid_after else "exited with code 9"
        ]


def test_child_reason_length_count_and_duplicates_are_bounded() -> None:
    payload = ["duplicate", "duplicate"] + [str(i) + "x" * 400 for i in range(30)]
    line = update._POST_UPDATE_FAILURE_MARKER + json.dumps(payload)
    with click.Context(update.update) as ctx:
        update._record_child_failures(
            subprocess.CompletedProcess([], 1, "", line + "\n" + line), fallback="fallback"
        )
        reasons = ctx.meta["update_audit_failures"]
        assert len(reasons) == 20
        assert len(set(reasons)) == 20
        assert max(map(len, reasons)) == 300
