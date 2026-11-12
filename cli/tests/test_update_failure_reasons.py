"""Offline regression tests for update failure diagnostics."""

from __future__ import annotations

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


@pytest.fixture(autouse=True)
def isolated_update(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_fake_home(monkeypatch, tmp_path)
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
def test_child_hook_emits_scrubbed_reasons(monkeypatch: pytest.MonkeyPatch, silent: bool) -> None:
    def fail(**kw: object) -> bool:
        update._record_audit_failure("Failed /private/home token=private")
        return False

    monkeypatch.setattr(update, "_run_post_update_tasks", fail)
    result = CliRunner().invoke(
        main, ["_post-update", "--expected-version", "1.0"] + (["--silent"] if silent else [])
    )
    assert result.exit_code == 1
    if silent:
        assert result.output == ""
    else:
        assert result.stdout == ""
        assert result.stderr.startswith(update._POST_UPDATE_FAILURE_MARKER)
        assert "private" not in result.output
        assert json.loads(result.stderr.split("=", 1)[1]) == ["Failed <path> token=<redacted>"]


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
