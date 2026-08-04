"""Error handling helpers for `inspire init` command execution."""

from __future__ import annotations

import io
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout


def run_init_action(func, json_mode: bool, *args, **kwargs) -> None:  # noqa: ANN001
    """Run an init action and normalize machine-mode failures to ValueError."""
    if not json_mode:
        func(*args, **kwargs)
        return

    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8") as child_stdout,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8") as child_stderr,
    ):
        try:
            os.dup2(child_stdout.fileno(), 1)
            os.dup2(child_stderr.fileno(), 2)
            try:
                with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
                    func(*args, **kwargs)
            except SystemExit as e:
                combined = "\n".join(
                    [stdout_buffer.getvalue(), stderr_buffer.getvalue()]
                )
                message = extract_error_message(combined) or f"Command exited with code {e.code}"
                raise ValueError(message) from e
        finally:
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
            os.close(saved_stdout)
            os.close(saved_stderr)


def extract_error_message(text: str) -> str:
    """Extract the most relevant error message from redirected output."""
    for line in reversed((text or "").splitlines()):
        trimmed = line.strip()
        if not trimmed:
            continue
        if trimmed.lower().startswith("error:"):
            return trimmed.split(":", 1)[1].strip()
        return trimmed
    return ""
