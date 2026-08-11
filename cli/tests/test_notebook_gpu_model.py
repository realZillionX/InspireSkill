from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from inspire.cli.commands.notebook import gpu_model as gpu_model_module


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


def test_probe_runs_once_per_notebook(monkeypatch: pytest.MonkeyPatch) -> None:
    """The model is fixed for the life of a notebook id, so one probe answers."""
    calls: list[str] = []

    def fake_run(**kwargs):  # noqa: ANN202
        calls.append(str(kwargs["notebook_id"]))
        return _Result(returncode=0, output="NVIDIA H200\n")

    monkeypatch.setattr(
        gpu_model_module.browser_api_module,
        "run_command_capture_in_notebook",
        fake_run,
    )

    assert gpu_model_module.notebook_gpu_model(notebook_id="nb-1", session=None) == "H200"
    assert gpu_model_module.notebook_gpu_model(notebook_id="nb-1", session=None) == "H200"
    assert gpu_model_module.notebook_gpu_model(notebook_id="nb-2", session=None) == "H200"

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
    assert cached["nb-1"]["gpu_model"] == "4090"
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
