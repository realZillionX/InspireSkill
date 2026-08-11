"""The GPU model a notebook actually runs on, read from the machine itself.

SSH/rtunnel is unavailable on H100/H200 machines, so every command that has to
pick between SSH and JupyterTerminal first needs to know which machine it is
talking to. A compute group's name usually carries the model, but it is a label
someone typed: it can be renamed, abbreviated, or simply disagree with the
hardware behind it. Ask the machine instead -- ``nvidia-smi`` over
JupyterTerminal, the one channel every notebook has whatever the policy is.

Opening a remote terminal costs a few seconds and the answer is needed before
almost every notebook command, so it is remembered on disk. A notebook's
resource spec is fixed when it is created, so the model reported for a notebook
id holds for as long as that id exists; entries expire only to keep the file
from growing without bound.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from inspire.platform.web import browser_api as browser_api_module

from .notebook_lookup import _normalize_gpu_type_for_display

if TYPE_CHECKING:
    from inspire.platform.web.session import WebSession

logger = logging.getLogger(__name__)

# One line per GPU, e.g. ``NVIDIA H200``. A machine without an NVIDIA GPU has no
# `nvidia-smi` at all and the command exits non-zero -- that is the "no GPU"
# answer, not a failed probe.
GPU_MODEL_PROBE_COMMAND = "nvidia-smi --query-gpu=name --format=csv,noheader"
PROBE_TIMEOUT_SECONDS = 60

# The machine answered and has no GPU. Distinct from ``None``, which means the
# machine did not answer at all.
NO_GPU = ""

CACHE_FILE = Path.home() / ".inspire" / "notebook-gpu-models.json"
CACHE_TTL_SECONDS = 30 * 24 * 3600

# A terminal writes cursor and title escapes around the command output; neither
# is part of a GPU name.
_TERMINAL_ESCAPE_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
)


def parse_gpu_model(output: str) -> str:
    """Return the first GPU model ``nvidia-smi`` named, as ``H200``-style text.

    A node's GPUs are homogeneous, so the first line answers for the machine.
    """
    for line in _TERMINAL_ESCAPE_RE.sub("", output).splitlines():
        model = _normalize_gpu_type_for_display(line.strip())
        if model:
            return model
    return NO_GPU


def notebook_gpu_model(
    *,
    notebook_id: str,
    session: Optional["WebSession"] = None,
    timeout: int = PROBE_TIMEOUT_SECONDS,
) -> str | None:
    """Return the notebook's GPU model, ``NO_GPU`` on a CPU machine.

    ``None`` means the machine could not be asked -- a stopped notebook, or a
    Jupyter server that did not come up -- which is not the same as having no
    GPU, so callers decide for themselves what an unread model implies.
    """
    handle = str(notebook_id or "").strip()
    if not handle:
        return None

    cached = _cached_gpu_model(handle)
    if cached is not None:
        return cached

    model = _probe_gpu_model(notebook_id=handle, session=session, timeout=timeout)
    if model is not None:
        _remember_gpu_model(handle, model)
    return model


def _probe_gpu_model(
    *,
    notebook_id: str,
    session: Optional["WebSession"],
    timeout: int,
) -> str | None:
    try:
        result = browser_api_module.run_command_capture_in_notebook(
            notebook_id=notebook_id,
            command=GPU_MODEL_PROBE_COMMAND,
            session=session,
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 - an unreadable model is an answer of its own
        logger.debug("Notebook GPU model probe failed", exc_info=True)
        return None

    if not result.completed:
        logger.debug("Notebook GPU model probe produced no terminal output")
        return None
    if result.returncode != 0:
        return NO_GPU
    return parse_gpu_model(result.output)


def _fresh(entry: dict[str, Any], *, now: float) -> bool:
    observed_at = entry.get("observed_at")
    if not isinstance(observed_at, (int, float)):
        return False
    return now - float(observed_at) < CACHE_TTL_SECONDS


def _read_cache() -> dict[str, Any]:
    try:
        with CACHE_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _cached_gpu_model(notebook_id: str) -> str | None:
    entry = _read_cache().get(notebook_id)
    if not isinstance(entry, dict) or not _fresh(entry, now=time.time()):
        return None
    model = entry.get("gpu_model")
    return model if isinstance(model, str) else None


def _remember_gpu_model(notebook_id: str, gpu_model: str) -> None:
    now = time.time()
    cache = {
        handle: entry
        for handle, entry in _read_cache().items()
        if isinstance(entry, dict) and _fresh(entry, now=now)
    }
    cache[notebook_id] = {"gpu_model": gpu_model, "observed_at": now}
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = CACHE_FILE.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(cache, handle, indent=2, sort_keys=True)
        temporary.replace(CACHE_FILE)
    except OSError:
        logger.debug("Notebook GPU model cache write failed", exc_info=True)
