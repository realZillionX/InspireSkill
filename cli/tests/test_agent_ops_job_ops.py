"""Offline regression coverage for the optional job event ledger example."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from uuid import UUID

import click
import pytest

from inspire.cli.main import main as cli_main


HELPER = Path(__file__).resolve().parents[2] / "examples" / "agent_ops" / "job_ops.py"

# This fake never imports Inspire or contacts the platform. All effects are in
# tmp_path. It can inspect preflight evidence before simulating a mutation.
FAKE_CLI = r"""
import json
import os
from pathlib import Path
import sys

expected = os.environ.get("JOB_OPS_EXPECT_ARGS")
if expected is not None and sys.argv[1:] != json.loads(expected):
    sys.exit(91)
ledger_name = os.environ.get("JOB_OPS_EXPECT_LEDGER")
if ledger_name:
    records = [json.loads(line) for line in Path(ledger_name).read_text().splitlines()]
    if not records or records[-1]["status"] != "pending":
        sys.exit(92)
with Path(os.environ["JOB_OPS_CALLS"]).open("a") as stream:
    stream.write("called\n")
mode = os.environ.get("JOB_OPS_MODE", "normal")
if mode == "prompt":
    sys.stdout.write("Delete job? [y/N]: ")
    sys.stdout.flush()
    answer = sys.stdin.readline().strip()
    print("deleted" if answer == "y" else "declined")
elif mode == "partial":
    print(json.dumps({"items": [{"name": "accepted-item"}],
                      "failures": [{"name": "rejected-item", "error": "private-detail"}]}))
else:
    print(os.environ.get("JOB_OPS_STDOUT", "fake CLI output"))
print("fake CLI stderr", file=sys.stderr)
if mode == "break-ledger":
    ledger = Path(ledger_name)
    ledger.rename(ledger.with_suffix(".pending"))
    ledger.mkdir()
elif mode == "block-recovery":
    parent = Path(ledger_name).parent
    parent.rename(parent.with_name(parent.name + ".saved"))
    parent.write_text("blocked", encoding="utf-8")
sys.exit(int(os.environ.get("JOB_OPS_RC", "0")))
"""


@pytest.fixture
def job_ops(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("_example_job_ops", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_cli(tmp_path: Path) -> dict[str, str]:
    binary_dir = tmp_path / "fake bin"
    binary_dir.mkdir()
    script = binary_dir / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    if os.name == "nt":
        launcher = binary_dir / "inspire.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        launcher = binary_dir / "inspire"
        launcher.write_text(f"#!{sys.executable}\n" + FAKE_CLI, encoding="utf-8")
        launcher.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir()
    # Do not inherit ambient credentials into subprocesses or pytest's fixture
    # diagnostics. Keep only OS runtime paths, then add the test's own settings.
    runtime_keys = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"}
    env = {key: value for key, value in os.environ.items() if key.upper() in runtime_keys}
    search_path = [str(binary_dir)]
    if os.name == "nt":
        search_path.append(str(Path(os.environ["SYSTEMROOT"]) / "System32"))
    env.update(
        {
            "PATH": os.pathsep.join(search_path),
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONIOENCODING": "utf-8",
            "JOB_OPS_CALLS": str(tmp_path / "calls.txt"),
        }
    )
    return env


def invoke(
    tmp_path: Path,
    env: dict[str, str],
    args: list[str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        input="",
        timeout=20,
    )


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def assert_command_evidence(events: list[dict], action: str, rc: int) -> None:
    assert len(events) == 2
    intent, outcome = events
    assert intent["status"] == "pending"
    assert outcome["status"] == "command_exited"
    assert outcome["rc"] == rc
    assert intent["operation_id"] == outcome["operation_id"]
    assert UUID(intent["operation_id"]).version == 4
    for event in events:
        assert event["action"] == action
        assert event["scope"] == "command"
        assert event["needs_reconcile"] is True
        assert (
            datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).tzinfo == timezone.utc
        )
        assert "succeeded" not in event.values()
        assert "success" not in event


@pytest.mark.parametrize("style", ["long", "short", "attached"])
def test_create_forwarding_and_allowlisted_metadata(
    tmp_path: Path,
    fake_cli: dict[str, str],
    style: str,
) -> None:
    ledger = tmp_path / "events.jsonl"
    aliases = {
        "long": ["--name", "training", "--project", "Project", "--quota", "8,80,800"],
        "short": ["-n", "training", "-p", "Project", "-q", "8,80,800"],
        "attached": ["-ntraining", "-pProject", "--quota=8,80,800"],
    }
    args = [
        *aliases[style],
        "--workspace=Workspace",
        "--account",
        "test-account",
        "--group",
        "Compute Group",
        "--nodes",
        "2",
        "--priority",
        "4",
        "-i",
        "image-private-value",
        "-c",
        "python train.py --token command-secret",
        "--env",
        "KEY=env-secret",
        "--description=description-secret",
    ]
    fake_cli.update(
        {
            "JOB_OPS_EXPECT_ARGS": json.dumps(["job", "create", *args]),
            "JOB_OPS_EXPECT_LEDGER": str(ledger),
            "INSPIRE_PASSWORD": "ambient-secret",
        }
    )
    result = invoke(tmp_path, fake_cli, ["create", *args, "--ledger", str(ledger)])
    assert result.returncode == 0, result.stderr
    events = records(ledger)
    assert_command_evidence(events, "create", 0)
    assert (
        events[0]["metadata"]
        == events[1]["metadata"]
        == {
            "account": "test-account",
            "workspace": "Workspace",
            "project": "Project",
            "name": "training",
            "group": "Compute Group",
            "quota": "8,80,800",
            "nodes": "2",
        }
    )
    text = ledger.read_text()
    for secret in [
        "command-secret",
        "env-secret",
        "ambient-secret",
        "image-private-value",
        "description-secret",
        "python train.py",
        "argv",
        "priority",
    ]:
        assert secret not in text
    assert result.stdout == "fake CLI output\n"
    assert result.stderr == "fake CLI stderr\n"


@pytest.mark.parametrize("action", ["create", "stop", "delete", "batch"])
def test_option_positions_follow_actual_click_schema(job_ops: ModuleType, action: str) -> None:
    """Every current value-taking option must protect an option-looking value.

    Click is the independent oracle: this also catches future schema drift that
    could silently consume a command, env value or account alias as a ledger flag.
    """
    command = cli_main.commands["job"].commands[action]
    for option in command.get_params(click.Context(command)):
        if not isinstance(option, click.Option):
            continue
        for spelling in [*option.opts, *option.secondary_opts]:
            args = [spelling] if option.is_flag else [spelling, "--ledger"]
            parsed = job_ops.parse_invocation([action, *args, "--ledger", "events.jsonl"])
            assert parsed.args == args
            assert parsed.ledger == "events.jsonl"
            if not option.is_flag:
                assert parsed.readonly is False
                assert option.nargs == 1


@pytest.mark.parametrize("value", ["--ledger", "--ledger=private-value", "--dry-run", "--help"])
@pytest.mark.parametrize("option", ["--command", "-c", "--env", "--description"])
def test_literal_option_values_are_forwarded_and_do_not_disable_logging(
    tmp_path: Path,
    fake_cli: dict[str, str],
    option: str,
    value: str,
) -> None:
    ledger = tmp_path / "events.jsonl"
    args = ["--name", "literal", option, value]
    fake_cli.update(
        {
            "JOB_OPS_EXPECT_ARGS": json.dumps(["job", "create", *args]),
            "JOB_OPS_EXPECT_LEDGER": str(ledger),
        }
    )
    result = invoke(tmp_path, fake_cli, ["create", *args, f"--ledger={ledger}"])
    assert result.returncode == 0, result.stderr
    assert_command_evidence(records(ledger), "create", 0)
    assert records(ledger)[0]["metadata"] == {"name": "literal"}
    assert "private-value" not in ledger.read_text()


@pytest.mark.parametrize(
    "action,target", [("stop", "--ledger"), ("delete", "--help"), ("batch", "--dry-run")]
)
def test_end_of_options_preserves_literal_names_and_paths(
    tmp_path: Path,
    fake_cli: dict[str, str],
    action: str,
    target: str,
) -> None:
    ledger = tmp_path / "events.jsonl"
    args = ["--", target]
    fake_cli["JOB_OPS_EXPECT_ARGS"] = json.dumps(["job", action, *args])
    result = invoke(tmp_path, fake_cli, ["--ledger", str(ledger), action, *args])
    assert result.returncode == 0, result.stderr
    events = records(ledger)
    assert_command_evidence(events, action, 0)
    if action == "batch":
        assert events[0]["plan_path"] == str((tmp_path / target).resolve())
    else:
        assert events[0]["metadata"]["name"] == target


@pytest.mark.parametrize(
    "ledger_args",
    [
        ["--ledger"],
        ["--ledger", ""],
        ["--ledger", "  "],
        ["--ledger="],
        ["--ledger=  "],
        ["--ledger", "--help"],
    ],
)
@pytest.mark.parametrize("prefix", [[], ["create"]])
def test_bad_ledger_path_fails_before_invocation(
    tmp_path: Path,
    fake_cli: dict[str, str],
    ledger_args: list[str],
    prefix: list[str],
) -> None:
    result = invoke(tmp_path, fake_cli, [*prefix, *ledger_args])
    assert result.returncode == 2
    assert not Path(fake_cli["JOB_OPS_CALLS"]).exists()
    assert not (Path(fake_cli["HOME"]) / ".inspire").exists()


@pytest.mark.parametrize("relative", ["events.jsonl", "nested/events.jsonl", "-events.jsonl"])
def test_relative_ledger_preserves_existing_bytes(
    tmp_path: Path,
    fake_cli: dict[str, str],
    relative: str,
) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"historical":"kept exactly"}\n'
    path.write_bytes(original)
    args = ["--workspace", "Workspace", "--pick=2", "run-name", "-y"]
    fake_cli["JOB_OPS_EXPECT_ARGS"] = json.dumps(["job", "delete", *args])
    result = invoke(tmp_path, fake_cli, ["delete", *args, f"--ledger={relative}"])
    assert result.returncode == 0, result.stderr
    assert path.read_bytes().startswith(original)
    events = records(path)
    assert len(events) == 3
    assert_command_evidence(events[1:], "delete", 0)
    assert events[1]["metadata"] == {"workspace": "Workspace", "pick": "2", "name": "run-name"}


def test_default_event_file_is_separate_from_human_ledger(
    tmp_path: Path,
    fake_cli: dict[str, str],
) -> None:
    local = Path(fake_cli["HOME"]) / ".inspire"
    local.mkdir()
    jobs = local / "JOBS.md"
    jobs.write_text("human task notes\n", encoding="utf-8")
    fake_cli["INSPIRE_JOBS_LEDGER"] = str(jobs)
    result = invoke(tmp_path, fake_cli, ["stop", "run-name", "--workspace", "Workspace"])
    assert result.returncode == 0, result.stderr
    assert jobs.read_text() == "human task notes\n"
    assert_command_evidence(records(local / "job-events.jsonl"), "stop", 0)


@pytest.mark.parametrize(
    "args",
    [
        ["--help"],
        ["create", "--help"],
        ["stop", "--help"],
        ["delete", "--help"],
        ["batch", "--help"],
        ["create", "--dry-run"],
        ["batch", "plan.json", "--dry-run"],
    ],
)
def test_help_and_dry_run_never_append(
    tmp_path: Path,
    fake_cli: dict[str, str],
    args: list[str],
) -> None:
    ledger = tmp_path / "events.jsonl"
    original = b"leave even an invalid ledger untouched"
    ledger.write_bytes(original)
    result = invoke(tmp_path, fake_cli, ["--ledger", str(ledger), *args])
    assert result.returncode == 0, result.stderr
    assert ledger.read_bytes() == original
    ledger.unlink()
    result = invoke(tmp_path, fake_cli, ["--ledger", str(ledger), *args])
    assert result.returncode == 0, result.stderr
    assert not ledger.exists()
    assert not list(tmp_path.glob("*.recovery.jsonl"))


@pytest.mark.parametrize("rc,output", [(0, "Error is just a literal word"), (9, "OK Job created")])
def test_exit_code_not_human_text_is_recorded(
    tmp_path: Path,
    fake_cli: dict[str, str],
    rc: int,
    output: str,
) -> None:
    ledger = tmp_path / "events.jsonl"
    fake_cli.update({"JOB_OPS_RC": str(rc), "JOB_OPS_STDOUT": output})
    result = invoke(tmp_path, fake_cli, ["stop", "run-name", "--ledger", str(ledger)])
    assert result.returncode == rc
    assert result.stdout == output + "\n"
    assert result.stderr == "fake CLI stderr\n"
    assert_command_evidence(records(ledger), "stop", rc)
    assert output not in ledger.read_text()


def test_missing_cli_does_not_leave_pending_intent(
    tmp_path: Path,
    fake_cli: dict[str, str],
) -> None:
    fake_cli["PATH"] = str(tmp_path / "missing-bin")
    ledger = tmp_path / "events.jsonl"
    result = invoke(tmp_path, fake_cli, ["create", "--ledger", str(ledger)])
    assert result.returncode == 127
    assert "not found on PATH" in result.stderr
    assert not ledger.exists()
    assert not Path(fake_cli["JOB_OPS_CALLS"]).exists()


def test_cli_is_resolved_at_each_invocation(
    job_ops: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = iter(["first-inspire", "second-inspire"])
    observed = []

    def which(name: str) -> str:
        assert name == "inspire"
        return next(paths)

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        observed.append(command[0])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(job_ops.shutil, "which", which)
    monkeypatch.setattr(job_ops.subprocess, "run", run)
    for _ in range(2):
        assert job_ops.main(["create", "--ledger", str(tmp_path / "events.jsonl")]) == 0
    assert observed == ["first-inspire", "second-inspire"]
    assert len({event["operation_id"] for event in records(tmp_path / "events.jsonl")}) == 2


def test_launch_failure_is_recorded_without_exception_secrets(
    job_ops: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(job_ops.shutil, "which", lambda _: "fake-inspire")

    def refused(*args: object, **kwargs: object) -> None:
        raise PermissionError("exception-secret")

    monkeypatch.setattr(job_ops.subprocess, "run", refused)
    ledger = tmp_path / "events.jsonl"
    assert job_ops.main(["create", "--ledger", str(ledger)]) == 126
    events = records(ledger)
    assert [event["status"] for event in events] == ["pending", "not_started"]
    assert events[-1]["rc"] == 126
    assert events[-1]["needs_reconcile"] is False
    assert "exception-secret" not in ledger.read_text() + capsys.readouterr().err


@pytest.mark.parametrize("failure", ["directory", "parent-file", "incomplete-record"])
def test_preflight_io_failure_prevents_action(
    tmp_path: Path,
    fake_cli: dict[str, str],
    failure: str,
) -> None:
    ledger = tmp_path / "events.jsonl"
    if failure == "directory":
        ledger.mkdir()
    elif failure == "parent-file":
        parent = tmp_path / "blocked"
        parent.write_text("keep this")
        ledger = parent / "events.jsonl"
    else:
        ledger.write_bytes(b'{"partial":')
    result = invoke(tmp_path, fake_cli, ["create", "--ledger", str(ledger)])
    assert result.returncode == 73
    assert "command was not started" in result.stderr
    assert not Path(fake_cli["JOB_OPS_CALLS"]).exists()
    if failure == "incomplete-record":
        assert ledger.read_bytes() == b'{"partial":'


@pytest.mark.parametrize("rc", [0, 7])
def test_batch_partial_outcome_never_claims_item_success(
    tmp_path: Path,
    fake_cli: dict[str, str],
    rc: int,
) -> None:
    ledger = tmp_path / "events.jsonl"
    plan = tmp_path / "plan.json"
    plan.write_text('{"command":"batch-command-secret","env":{"KEY":"batch-env-secret"}}')
    args = ["-n", "1", "plan.json", "--account", "test-account"]
    fake_cli.update(
        {
            "JOB_OPS_MODE": "partial",
            "JOB_OPS_RC": str(rc),
            "JOB_OPS_EXPECT_ARGS": json.dumps(["job", "batch", *args]),
            "JOB_OPS_EXPECT_LEDGER": str(ledger),
        }
    )
    result = invoke(tmp_path, fake_cli, ["batch", *args, "--ledger", str(ledger)])
    assert result.returncode == rc, result.stderr
    assert "accepted-item" in result.stdout and "rejected-item" in result.stdout
    events = records(ledger)
    assert_command_evidence(events, "batch", rc)
    assert events[0]["plan_path"] == events[1]["plan_path"] == str(plan.resolve())
    assert events[0]["metadata"] == {"account": "test-account"}
    for text in [
        "accepted-item",
        "rejected-item",
        "private-detail",
        "batch-command-secret",
        "batch-env-secret",
    ]:
        assert text not in ledger.read_text()


@pytest.mark.parametrize("answer,rc", [("n", 0), ("y", 6)])
def test_prompt_is_visible_before_input_and_exit_code_survives(
    tmp_path: Path,
    fake_cli: dict[str, str],
    answer: str,
    rc: int,
) -> None:
    ledger = tmp_path / "events.jsonl"
    fake_cli.update(
        {"JOB_OPS_MODE": "prompt", "JOB_OPS_RC": str(rc), "JOB_OPS_EXPECT_LEDGER": str(ledger)}
    )
    with subprocess.Popen(
        [sys.executable, str(HELPER), "delete", "run-name", "--ledger", str(ledger)],
        cwd=tmp_path,
        env=fake_cli,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ) as process:
        assert process.stdout is not None
        prompt: queue.Queue[str] = queue.Queue()
        reader = threading.Thread(
            target=lambda: prompt.put(process.stdout.read(len("Delete job? [y/N]: "))),
            daemon=True,
        )
        reader.start()
        try:
            # No input has been sent yet: capturing/buffering child output in the
            # wrapper would deadlock here, even if communicate(input=...) passed.
            assert prompt.get(timeout=10) == "Delete job? [y/N]: "
            reader.join(timeout=1)
            stdout, stderr = process.communicate(answer + "\n", timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
    assert process.returncode == rc
    assert stdout == ("declined\n" if answer == "n" else "deleted\n")
    assert stderr == "fake CLI stderr\n"
    assert_command_evidence(records(ledger), "delete", rc)


WRITER = r"""
from pathlib import Path
import runpy
import sys

append = runpy.run_path(sys.argv[1])["append_event"]
print("ready", flush=True)
sys.stdin.read(1)
for index in range(24):
    append(Path(sys.argv[2]), {"writer": int(sys.argv[3]), "index": index,
                              "payload": "x" * 8192})
"""


@pytest.mark.parametrize("existing", [False, True])
def test_eight_processes_append_without_lost_or_duplicated_records(
    tmp_path: Path,
    existing: bool,
) -> None:
    ledger = tmp_path / "events.jsonl"
    original = b'{"historical":"unchanged"}\n' if existing else b""
    if existing:
        ledger.write_bytes(original)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", WRITER, str(HELPER), str(ledger), str(index)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        for index in range(8)
    ]
    try:
        ready: queue.Queue[str] = queue.Queue()
        readers = []
        for process in processes:
            assert process.stdout is not None
            reader = threading.Thread(
                target=lambda stream=process.stdout: ready.put(stream.readline()),
                daemon=True,
            )
            reader.start()
            readers.append(reader)
        # Wait until all eight imports have completed before releasing anyone.
        # Merely spawning eight processes need not make their writes overlap.
        for _ in processes:
            assert ready.get(timeout=15) == "ready\n"
        for reader in readers:
            reader.join(timeout=1)
        for process in processes:
            assert process.stdin is not None
            process.stdin.write("g")
            process.stdin.flush()
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr
            assert stdout == ""
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
    assert ledger.read_bytes().startswith(original)
    events = records(ledger)
    if existing:
        assert events.pop(0) == {"historical": "unchanged"}
    assert len(events) == 8 * 24
    assert Counter((event["writer"], event["index"]) for event in events) == Counter(
        (worker, index) for worker in range(8) for index in range(24)
    )
    assert all(event["payload"] == "x" * 8192 for event in events)


LOCK_HOLDER = r"""
import os
import sys

with open(sys.argv[1], "a+b") as stream:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    print("locked", flush=True)
    sys.stdin.read()
"""


def test_os_lock_blocks_mutation_with_bounded_timeout_and_releases_on_exit(
    tmp_path: Path,
    fake_cli: dict[str, str],
) -> None:
    ledger = tmp_path / "events.jsonl"
    with subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER, str(ledger)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as holder:
        assert holder.stdout is not None
        ready: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: ready.put(holder.stdout.readline()), daemon=True).start()
        try:
            assert ready.get(timeout=10) == "locked\n"
            started = time.monotonic()
            result = invoke(tmp_path, fake_cli, ["stop", "run-name", "--ledger", str(ledger)])
            elapsed = time.monotonic() - started
            assert result.returncode == 73, result.stderr
            assert "TimeoutError" in result.stderr
            assert 4 <= elapsed < 15
            assert not Path(fake_cli["JOB_OPS_CALLS"]).exists()
        finally:
            holder.kill()
            holder.communicate(timeout=10)
    assert ledger.read_bytes() == b""
    result = invoke(tmp_path, fake_cli, ["stop", "run-name", "--ledger", str(ledger)])
    assert result.returncode == 0, result.stderr
    assert_command_evidence(records(ledger), "stop", 0)


@pytest.mark.parametrize("mode", ["break-ledger", "block-recovery"])
@pytest.mark.parametrize("rc", [0, 8])
def test_post_action_io_failure_warns_and_preserves_recovery_without_retrying(
    tmp_path: Path,
    fake_cli: dict[str, str],
    mode: str,
    rc: int,
) -> None:
    ledger = tmp_path / "ledger-dir" / "events.jsonl"
    fake_cli.update(
        {"JOB_OPS_MODE": mode, "JOB_OPS_RC": str(rc), "JOB_OPS_EXPECT_LEDGER": str(ledger)}
    )
    result = invoke(tmp_path, fake_cli, ["create", "-n", "training", "--ledger", str(ledger)])
    assert result.returncode == 74
    assert "action may have succeeded" in result.stderr
    assert "do not retry blindly" in result.stderr
    assert result.stdout == "fake CLI output\n"
    assert "fake CLI stderr" in result.stderr
    assert Path(fake_cli["JOB_OPS_CALLS"]).read_text() == "called\n"
    if mode == "break-ledger":
        intent = records(ledger.with_suffix(".pending"))[0]
        recovery_files = list(ledger.parent.glob("*.recovery.jsonl"))
        assert len(recovery_files) == 1
        recovery = records(recovery_files[0])[0]
    else:
        intent = records(tmp_path / "ledger-dir.saved" / "events.jsonl")[0]
        line = next(
            line for line in result.stderr.splitlines() if line.startswith("job_ops recovery: ")
        )
        recovery = json.loads(line.removeprefix("job_ops recovery: "))
        assert (tmp_path / "ledger-dir").read_text() == "blocked"
    assert recovery["operation_id"] == intent["operation_id"]
    assert recovery["status"] == "command_exited"
    assert recovery["rc"] == rc
    assert recovery["needs_reconcile"] is True


def test_uncertain_fsync_does_not_retry_append_or_overwrite_recovery(
    job_ops: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = tmp_path / "events.jsonl"
    operation_id = "ae1c301d-7b58-4c9a-83b3-122d71289eae"
    recovery = tmp_path / f"events.jsonl.{operation_id}.recovery.jsonl"
    recovery.write_bytes(b"do not overwrite existing evidence\n")
    monkeypatch.setattr(job_ops.uuid, "uuid4", lambda: UUID(operation_id))
    monkeypatch.setattr(job_ops.shutil, "which", lambda _: "fake-inspire")
    monkeypatch.setattr(job_ops.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    real_fsync = os.fsync
    sync_count = 0

    def uncertain_sync(fd: int) -> None:
        nonlocal sync_count
        sync_count += 1
        if sync_count == 2:
            raise OSError("sensitive disk error details")
        real_fsync(fd)

    monkeypatch.setattr(job_ops.os, "fsync", uncertain_sync)
    assert job_ops.main(["create", "--ledger", str(ledger)]) == 74
    # The outcome already reached the file before fsync failed. Retrying the
    # append would duplicate it, and opening recovery in 'w' would lose evidence.
    assert_command_evidence(records(ledger), "create", 0)
    assert recovery.read_bytes() == b"do not overwrite existing evidence\n"
    error_output = capsys.readouterr().err
    assert "do not retry blindly" in error_output
    assert "job_ops recovery: " in error_output
    assert "sensitive disk error details" not in error_output + ledger.read_text()
