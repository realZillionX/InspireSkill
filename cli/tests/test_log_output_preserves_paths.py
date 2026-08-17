"""A log line's own filesystem paths must survive the JSON projection.

The shared sanitizer redacts absolute paths so platform handles never reach
public output. A log message is not a handle — it is the program's own words,
and `/bin/bash`, `/opt/conda/.../site.py` and `/usr/lib/...` in a traceback are
exactly what `logs` exists to show. Redacting them also made the two output
modes disagree: the human renderer only runs `scrub_raw_ids`, which leaves
paths alone, so the same line read differently with and without `--json`.
"""

from __future__ import annotations

from inspire.cli.commands.job.job_logs import (
    LOG_TEXT_KEYS,
    _format_web_log_line,
    _public_web_logs,
)
from inspire.cli.formatters import json_formatter


_TRACEBACK = 'File "/opt/conda/lib/python3.11/site.py", line 12, in <module>'
_SHELL = "+ /bin/bash -c 'python train.py'"


def test_public_logs_keep_paths_inside_the_message() -> None:
    rows = [{"timestamp_str": "t1", "pod_name": "worker-0", "message": _SHELL}]

    public = _public_web_logs(rows)

    assert public[0]["message"] == _SHELL


def test_public_logs_keep_traceback_paths() -> None:
    rows = [{"timestamp_str": "t1", "pod_name": "worker-0", "message": _TRACEBACK}]

    assert _public_web_logs(rows)[0]["message"] == _TRACEBACK


def test_json_and_human_render_the_same_message_text() -> None:
    """The two output modes must not disagree about one log line."""
    row = {"timestamp_str": "t1", "pod_name": "worker-0", "message": _TRACEBACK}

    human = _format_web_log_line(row)
    public = _public_web_logs([row])[0]["message"]

    assert public in human


def test_paths_outside_the_message_are_still_redacted() -> None:
    """The exemption is scoped to the log text, not to the whole record."""
    rows = [{"timestamp_str": "t1", "source_path": "/etc/passwd", "message": _SHELL}]

    public = _public_web_logs(rows)[0]

    assert public["message"] == _SHELL
    assert public["source_path"] == "<redacted>"


def test_log_text_keys_cover_every_spelling_the_renderer_reads() -> None:
    """`_format_web_log_line` falls back across these keys; all must be exempt."""
    assert {"message", "log", "content"} <= set(LOG_TEXT_KEYS)


def test_format_json_preserves_paths_for_the_log_payload() -> None:
    payload = {"logs": [{"message": _SHELL}], "shown": 1}

    rendered = json_formatter.format_json(payload, preserve_paths=LOG_TEXT_KEYS)

    assert "/bin/bash" in rendered
    assert "<redacted>" not in rendered


def test_format_json_still_redacts_paths_without_the_opt_in() -> None:
    """Guard the default: the exemption must be an explicit choice."""
    rendered = json_formatter.format_json({"logs": [{"message": _SHELL}]})

    assert "<redacted>" in rendered
