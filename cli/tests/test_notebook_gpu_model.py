from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from inspire.cli.commands.notebook import gpu_model as gpu_model_module
from inspire.accounts import storage
from multiprocess_workers import adopt_home, run_workers, worker_context


class _Result:
    def __init__(self, *, returncode: int, output: str, completed: bool = True) -> None:
        self.returncode = returncode
        self.output = output
        self.completed = completed


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        gpu_model_module,
        "CACHE_FILE",
        tmp_path / ".inspire" / "notebook-gpu-models.json",
    )


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("NVIDIA H200\nNVIDIA H200\n", "H200"),
        ("NVIDIA H100 80GB HBM3\n", "H100"),
        ("NVIDIA GeForce RTX 4090\n", "4090"),
        ("NVIDIA A100-SXM4-80GB\n", "A100"),
        # A terminal wraps the answer in cursor and title escapes.
        ("\x1b[?2004l\r\x1b]0;user@host\x07NVIDIA H200\r\n", "H200"),
        ("", ""),
    ],
)
def test_parse_gpu_model(output: str, expected: str) -> None:
    assert gpu_model_module.parse_gpu_model(output) == expected


def test_probe_reads_the_model_off_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_run(**kwargs):  # noqa: ANN202
        calls.append(kwargs)
        return _Result(returncode=0, output="NVIDIA H200\n")

    monkeypatch.setattr(
        gpu_model_module.browser_api_module,
        "run_command_capture_in_notebook",
        fake_run,
    )

    assert gpu_model_module.notebook_gpu_model(notebook_id="nb-1", session=None) == "H200"
    assert calls[0]["command"] == gpu_model_module.GPU_MODEL_PROBE_COMMAND


def test_probe_reports_no_gpu_when_nvidia_smi_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gpu_model_module.browser_api_module,
        "run_command_capture_in_notebook",
        lambda **_k: _Result(returncode=127, output="bash: nvidia-smi: command not found\n"),
    )

    assert gpu_model_module.notebook_gpu_model(notebook_id="nb-cpu", session=None) == ""


@pytest.mark.parametrize(
    "result",
    [
        _Result(returncode=124, output="", completed=False),
        None,  # the call itself blew up
    ],
)
def test_probe_returns_none_when_the_machine_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
    result: _Result | None,
) -> None:
    def fake_run(**_kwargs):  # noqa: ANN202
        if result is None:
            raise RuntimeError("no terminal")
        return result

    monkeypatch.setattr(
        gpu_model_module.browser_api_module,
        "run_command_capture_in_notebook",
        fake_run,
    )

    assert gpu_model_module.notebook_gpu_model(notebook_id="nb-stopped", session=None) is None


def test_one_probe_answers_for_the_whole_compute_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group is one pool of identical machines, so it is the cache key."""
    calls: list[str] = []

    def fake_run(**kwargs):  # noqa: ANN202
        calls.append(str(kwargs["notebook_id"]))
        return _Result(returncode=0, output="NVIDIA H200\n")

    monkeypatch.setattr(
        gpu_model_module.browser_api_module,
        "run_command_capture_in_notebook",
        fake_run,
    )
    probe = gpu_model_module.notebook_gpu_model

    assert probe(notebook_id="nb-1", compute_group="训练区-H200-1号机房") == "H200"
    assert probe(notebook_id="nb-2", compute_group="训练区-H200-1号机房") == "H200"
    assert probe(notebook_id="nb-3", compute_group="开发区-H100-119核") == "H200"

    # Only the first notebook in each group reached the machine.
    assert calls == ["nb-1", "nb-3"]


def test_a_notebook_without_a_group_falls_back_to_its_own_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_run(**kwargs):  # noqa: ANN202
        calls.append(str(kwargs["notebook_id"]))
        return _Result(returncode=0, output="NVIDIA H200\n")

    monkeypatch.setattr(
        gpu_model_module.browser_api_module,
        "run_command_capture_in_notebook",
        fake_run,
    )

    assert gpu_model_module.notebook_gpu_model(notebook_id="nb-1") == "H200"
    assert gpu_model_module.notebook_gpu_model(notebook_id="nb-1") == "H200"
    assert gpu_model_module.notebook_gpu_model(notebook_id="nb-2") == "H200"

    assert calls == ["nb-1", "nb-2"]


def test_a_machine_without_a_gpu_is_cached_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """`no GPU` is an answer; re-probing it on every command is the whole cost."""
    calls: list[str] = []

    def fake_run(**kwargs):  # noqa: ANN202
        calls.append(str(kwargs["notebook_id"]))
        return _Result(returncode=127, output="bash: nvidia-smi: command not found\n")

    monkeypatch.setattr(
        gpu_model_module.browser_api_module,
        "run_command_capture_in_notebook",
        fake_run,
    )

    assert gpu_model_module.notebook_gpu_model(notebook_id="nb-cpu", session=None) == ""
    assert gpu_model_module.notebook_gpu_model(notebook_id="nb-cpu", session=None) == ""

    assert calls == ["nb-cpu"]


def test_an_unread_model_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = [
        _Result(returncode=124, output="", completed=False),
        _Result(returncode=0, output="NVIDIA H200\n"),
    ]
    monkeypatch.setattr(
        gpu_model_module.browser_api_module,
        "run_command_capture_in_notebook",
        lambda **_k: answers.pop(0),
    )

    assert gpu_model_module.notebook_gpu_model(notebook_id="nb-1", session=None) is None
    assert gpu_model_module.notebook_gpu_model(notebook_id="nb-1", session=None) == "H200"


def test_expired_entries_are_reprobed_and_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = time.time() - gpu_model_module.CACHE_TTL_SECONDS - 1
    gpu_model_module.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    gpu_model_module.CACHE_FILE.write_text(
        json.dumps(
            {
                "nb-1": {"gpu_model": "H200", "observed_at": stale},
                "nb-gone": {"gpu_model": "4090", "observed_at": stale},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        gpu_model_module.browser_api_module,
        "run_command_capture_in_notebook",
        lambda **_k: _Result(returncode=0, output="NVIDIA GeForce RTX 4090\n"),
    )

    assert gpu_model_module.notebook_gpu_model(notebook_id="nb-1", session=None) == "4090"

    cached = json.loads(gpu_model_module.CACHE_FILE.read_text(encoding="utf-8"))
    assert len(cached) == 1
    assert next(iter(cached.values()))["gpu_model"] == "4090"
    assert "nb-gone" not in cached


def test_an_unreadable_cache_file_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    gpu_model_module.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    gpu_model_module.CACHE_FILE.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(
        gpu_model_module.browser_api_module,
        "run_command_capture_in_notebook",
        lambda **_k: _Result(returncode=0, output="NVIDIA H100\n"),
    )

    assert gpu_model_module.notebook_gpu_model(notebook_id="nb-1", session=None) == "H100"


def test_cache_status_and_clear_report_what_is_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gpu_model_module.browser_api_module,
        "run_command_capture_in_notebook",
        lambda **_k: _Result(returncode=0, output="NVIDIA H200\n"),
    )
    assert gpu_model_module.gpu_model_cache_status() == (0, 0.0)

    gpu_model_module.notebook_gpu_model(notebook_id="nb-1", compute_group="训练区")
    gpu_model_module.notebook_gpu_model(notebook_id="nb-2", compute_group="开发区")

    count, observed_at = gpu_model_module.gpu_model_cache_status()
    assert count == 2
    assert observed_at > 0

    assert gpu_model_module.clear_gpu_model_cache() == 2
    assert gpu_model_module.gpu_model_cache_status() == (0, 0.0)
    assert gpu_model_module.clear_gpu_model_cache() == 0


def test_gpu_cache_is_scoped_by_account_and_platform(monkeypatch):
    calls = []

    def probe(**kwargs):
        calls.append((kwargs["session"].account, kwargs["session"].base_url))
        return "H200" if kwargs["session"].account == "alice" else "4090"

    monkeypatch.setattr(gpu_model_module, "_probe_gpu_model", probe)
    alice = SimpleNamespace(account="alice", base_url="https://platform.example")
    bob = SimpleNamespace(account="bob", base_url="https://platform.example")
    staging = SimpleNamespace(account="bob", base_url="https://staging.example")
    read = gpu_model_module.notebook_gpu_model
    assert read(notebook_id="nb-1", compute_group="same-group", session=alice) == "H200"
    assert read(notebook_id="nb-1", compute_group="same-group", session=bob) == "4090"
    assert read(notebook_id="nb-1", compute_group="same-group", session=staging) == "4090"
    assert len(calls) == 3
    with storage.account_scope("alice"):
        assert gpu_model_module.gpu_model_cache_status()[0] == 1
        assert gpu_model_module.clear_gpu_model_cache() == 1
    with storage.account_scope("bob"):
        assert gpu_model_module.gpu_model_cache_status()[0] == 2
    assert read(notebook_id="nb-1", compute_group="same-group", session=bob) == "4090"
    assert len(calls) == 3


def test_unscoped_gpu_cache_does_not_choose_a_named_accounts_transport(monkeypatch):
    path = gpu_model_module.CACHE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {"gpu_model": "H200", "observed_at": time.time()}
    path.write_text(json.dumps({"same-group": legacy}))
    monkeypatch.setattr(gpu_model_module, "_probe_gpu_model", lambda **_k: "4090")
    assert gpu_model_module.notebook_gpu_model(
        notebook_id="nb-1", compute_group="same-group",
        session=SimpleNamespace(account="bob", base_url="https://platform.example"),
    ) == "4090"
    # Existing entries remain until their normal expiry; they are not assigned
    # to another account merely because that account ran the next command.
    assert json.loads(path.read_text())["same-group"] == legacy


def _write_gpu_model(index, home, barrier):
    adopt_home(home)
    gpu_model_module.CACHE_FILE = Path(home) / "notebook-gpu-models.json"
    barrier.wait(timeout=15)
    gpu_model_module._remember_gpu_model(f"group-{index}", "H200", account=f"account-{index}")


def test_concurrent_accounts_preserve_all_gpu_cache_entries(tmp_path):
    context = worker_context()
    count = 8
    barrier = context.Barrier(count)
    codes = run_workers(
        context, _write_gpu_model, count=count,
        args_for=lambda index: (index, str(tmp_path), barrier),
    )
    assert codes == [0] * count
    entries = json.loads((tmp_path / "notebook-gpu-models.json").read_text())
    assert set(entries) == {f"group-{index}" for index in range(count)}
