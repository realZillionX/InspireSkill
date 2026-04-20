"""Helpers for notebook post-start commands and scripts."""

from __future__ import annotations

import base64
import shlex
from dataclasses import dataclass
from pathlib import Path

from inspire.config.models import Config

POST_START_LOG = "/tmp/inspire-notebook-post-start.log"
POST_START_PID_FILE = "/tmp/inspire-notebook-post-start.pid"
POST_START_SCRIPT_PATH = "/tmp/inspire-notebook-post-start.sh"
POST_START_STARTED_MARKER = "INSPIRE_NOTEBOOK_POST_START_STARTED"
NO_WAIT_POST_START_WARNING = (
    "Note: --no-wait requested, but a notebook post-start action is configured. "
    "Waiting anyway so the post-start action can run. "
    "Remove the post-start action or set notebook_post_start=none to return immediately."
)
_POST_START_DISABLED_VALUES = {"0", "disable", "disabled", "false", "none", "off"}
_REMOVED_POST_START_VALUES = {"keepalive"}


@dataclass(frozen=True)
class NotebookPostStartSpec:
    label: str
    command: str
    log_path: str
    pid_file: str
    completion_marker: str
    requires_gpu: bool = False


def _normalize_post_start_value(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _is_disabled_post_start(value: str | None) -> bool:
    text = _normalize_post_start_value(value)
    return bool(text and text.lower() in _POST_START_DISABLED_VALUES)


def _is_removed_post_start_value(value: str | None) -> bool:
    text = _normalize_post_start_value(value)
    return bool(text and text.lower() in _REMOVED_POST_START_VALUES)


def _build_background_command(
    command_text: str,
    *,
    log_path: str,
    pid_file: str,
    completion_marker: str,
) -> str:
    pid_file_q = shlex.quote(pid_file)
    marker_q = shlex.quote(completion_marker)
    inner_command = f"echo $$ > {pid_file_q}; exec bash -lc {shlex.quote(command_text)}"
    return " ".join(
        [
            f'if [ -f {pid_file_q} ] && kill -0 "$(cat {pid_file_q})" 2>/dev/null; then',
            f"echo {marker_q};",
            "exit 0;",
            "fi;",
            f"rm -f {pid_file_q};",
            f"nohup bash -lc {shlex.quote(inner_command)} > {shlex.quote(log_path)} 2>&1 < /dev/null &",
            "POST_START_STARTED=0;",
            "for _ in 1 2 3 4 5; do",
            f'if [ -f {pid_file_q} ] && kill -0 "$(cat {pid_file_q})" 2>/dev/null; then POST_START_STARTED=1;',
            f"echo {marker_q};",
            "break;",
            "fi;",
            "sleep 1;",
            "done;",
            '[ "$POST_START_STARTED" -eq 1 ]',
        ]
    )


def _build_script_command(
    script_text: str,
    *,
    log_path: str,
    pid_file: str,
    completion_marker: str,
) -> str:
    encoded_script = base64.b64encode(script_text.encode("utf-8")).decode("ascii")
    command_text = (
        f"SCRIPT_PATH={shlex.quote(POST_START_SCRIPT_PATH)}; "
        f'printf %s {shlex.quote(encoded_script)} | base64 -d > "$SCRIPT_PATH"; '
        'chmod +x "$SCRIPT_PATH"; '
        'exec bash "$SCRIPT_PATH"'
    )
    return _build_background_command(
        command_text,
        log_path=log_path,
        pid_file=pid_file,
        completion_marker=completion_marker,
    )


def _build_command_spec(command_text: str) -> NotebookPostStartSpec:
    return NotebookPostStartSpec(
        label="notebook post-start command",
        command=_build_background_command(
            command_text,
            log_path=POST_START_LOG,
            pid_file=POST_START_PID_FILE,
            completion_marker=POST_START_STARTED_MARKER,
        ),
        log_path=POST_START_LOG,
        pid_file=POST_START_PID_FILE,
        completion_marker=POST_START_STARTED_MARKER,
    )


def _build_script_spec(script_path: Path) -> NotebookPostStartSpec:
    try:
        script_text = script_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Failed to read post-start script '{script_path}': {exc}") from exc

    if not script_text.strip():
        raise ValueError(f"Post-start script '{script_path}' is empty.")

    return NotebookPostStartSpec(
        label=f"notebook post-start script ({script_path.name})",
        command=_build_script_command(
            script_text,
            log_path=POST_START_LOG,
            pid_file=POST_START_PID_FILE,
            completion_marker=POST_START_STARTED_MARKER,
        ),
        log_path=POST_START_LOG,
        pid_file=POST_START_PID_FILE,
        completion_marker=POST_START_STARTED_MARKER,
    )


def resolve_notebook_post_start_spec(
    *,
    config: Config,
    post_start: str | None,
    post_start_script: Path | None,
    keepalive: bool | None = None,
) -> NotebookPostStartSpec | None:
    if keepalive is not None:
        raise ValueError(
            "The keepalive notebook post-start preset has been removed. "
            "Use post_start='none', --post-start '<shell command>', "
            "or --post-start-script PATH."
        )

    if post_start_script is not None:
        return _build_script_spec(post_start_script)

    resolved_value = _normalize_post_start_value(post_start)
    if resolved_value is None:
        resolved_value = _normalize_post_start_value(getattr(config, "notebook_post_start", None))

    if _is_disabled_post_start(resolved_value):
        return None
    if _is_removed_post_start_value(resolved_value):
        raise ValueError(
            "The 'keepalive' notebook post-start preset has been removed. "
            "Use notebook_post_start=none, --post-start '<shell command>', "
            "or --post-start-script PATH."
        )
    if resolved_value is None:
        return None
    return _build_command_spec(resolved_value)


__all__ = [
    "NotebookPostStartSpec",
    "NO_WAIT_POST_START_WARNING",
    "POST_START_LOG",
    "POST_START_PID_FILE",
    "POST_START_SCRIPT_PATH",
    "POST_START_STARTED_MARKER",
    "resolve_notebook_post_start_spec",
]
