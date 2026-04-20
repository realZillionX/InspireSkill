"""Debug logging setup for InspireSkill's CLI layer.

Creates per-run debug report files, applies best-effort redaction, and
attaches file handlers for all ``inspire.*`` loggers.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from inspire import __version__

DEFAULT_DEBUG_LOG_LIMIT = 20
DEFAULT_DEBUG_LOG_DIR = Path.home() / ".cache" / "inspire-skill" / "logs"
DEBUG_LOG_DIR_ENV = "INSPIRE_DEBUG_LOG_DIR"

_DEBUG_HANDLER_MARKER = "_inspire_debug_handler"
_DEBUG_PREV_LEVEL = "_inspire_debug_prev_level"
_DEBUG_PREV_PROPAGATE = "_inspire_debug_prev_propagate"
_MISSING = object()

_SENSITIVE_FIELD_RE = re.compile(
    r"""(?ix)
    (
        [\"']?
        (?:
            password|passwd|token|access[_-]?token|refresh[_-]?token|
            secret|api[_-]?key|authorization|cookie|set-cookie
        )
        [\"']?
        \s*[:=]\s*
    )
    (
        \"[^\"]*\" | '[^']*' | [^\s,}\]]+
    )
    """
)
_QUERY_TOKEN_RE = re.compile(r"(?i)([?&](?:token|access_token|refresh_token)=)([^&\s]+)")
_PATH_TOKEN_RE = re.compile(r"(/(?:jupyter|vscode)/[^/]+/)([^/]+)(/proxy/)")
_AUTH_BEARER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer|token)\s+([^\s,]+)")


def redact_text(text: str) -> str:
    """Best-effort redaction for secrets in log text."""
    value = str(text or "")
    if not value:
        return value

    value = _AUTH_BEARER_RE.sub(r"\1\2 <redacted>", value)
    value = _SENSITIVE_FIELD_RE.sub(r"\1<redacted>", value)
    value = _QUERY_TOKEN_RE.sub(r"\1<redacted>", value)
    value = _PATH_TOKEN_RE.sub(r"\1<redacted>\3", value)
    return value


class _RedactingFormatter(logging.Formatter):
    """Formatter that redacts sensitive text in final rendered log lines."""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return redact_text(rendered)


def _resolve_debug_log_dir() -> Path:
    override = os.getenv(DEBUG_LOG_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_DEBUG_LOG_DIR


def _remove_previous_debug_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, _DEBUG_HANDLER_MARKER, False):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


def _stash_logger_state(logger: logging.Logger) -> None:
    if not hasattr(logger, _DEBUG_PREV_LEVEL):
        setattr(logger, _DEBUG_PREV_LEVEL, logger.level)
    if not hasattr(logger, _DEBUG_PREV_PROPAGATE):
        setattr(logger, _DEBUG_PREV_PROPAGATE, logger.propagate)


def _restore_logger_state(logger: logging.Logger) -> None:
    previous_level = getattr(logger, _DEBUG_PREV_LEVEL, _MISSING)
    if previous_level is not _MISSING:
        logger.setLevel(previous_level)
        delattr(logger, _DEBUG_PREV_LEVEL)

    previous_propagate = getattr(logger, _DEBUG_PREV_PROPAGATE, _MISSING)
    if previous_propagate is not _MISSING:
        logger.propagate = previous_propagate
        delattr(logger, _DEBUG_PREV_PROPAGATE)


def clear_debug_logging() -> None:
    """Detach and close existing debug handlers from the ``inspire`` logger."""
    inspire_logger = logging.getLogger("inspire")
    _remove_previous_debug_handlers(inspire_logger)
    _restore_logger_state(inspire_logger)


def _prune_old_debug_logs(log_dir: Path, *, keep: int, preserve: Path | None = None) -> None:
    if keep < 1:
        keep = 1

    files = sorted(
        log_dir.glob("inspire-debug-*.log"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if preserve is not None:
        preserved = next((path for path in files if path == preserve), None)
        if preserved is not None:
            files = [preserved] + [path for path in files if path != preserved]

    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            continue


def _build_debug_log_path(log_dir: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    nonce = uuid.uuid4().hex[:8]
    return log_dir / f"inspire-debug-{timestamp}-{os.getpid()}-{nonce}.log"


def _session_header(argv: Sequence[str] | None = None) -> Iterable[str]:
    run_argv = list(argv) if argv is not None else list(sys.argv)
    yield "Debug session started"
    yield f"version={__version__}"
    yield f"python={sys.version.split()[0]}"
    yield f"platform={platform.platform()}"
    yield f"cwd={Path.cwd()}"
    yield f"argv={run_argv}"
    yield f"utc={datetime.now(timezone.utc).isoformat()}"


def configure_debug_logging(
    *,
    argv: Sequence[str] | None = None,
    keep_logs: int = DEFAULT_DEBUG_LOG_LIMIT,
) -> str | None:
    """Enable debug logging to a per-run report file.

    Returns:
        Absolute log file path on success, otherwise ``None``.
    """
    try:
        log_dir = _resolve_debug_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        _prune_old_debug_logs(log_dir, keep=keep_logs)
        log_path = _build_debug_log_path(log_dir)

        inspire_logger = logging.getLogger("inspire")
        _stash_logger_state(inspire_logger)
        _remove_previous_debug_handlers(inspire_logger)

        handler = logging.FileHandler(log_path, encoding="utf-8")
        setattr(handler, _DEBUG_HANDLER_MARKER, True)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(
            _RedactingFormatter(
                fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

        inspire_logger.addHandler(handler)
        inspire_logger.setLevel(logging.DEBUG)
        inspire_logger.propagate = False

        header_logger = logging.getLogger("inspire.cli.debug")
        for line in _session_header(argv):
            header_logger.debug(line)
        header_logger.debug("debug_report=%s", log_path)
        _prune_old_debug_logs(log_dir, keep=keep_logs, preserve=log_path)
        return str(log_path)
    except Exception:
        return None


__all__ = [
    "clear_debug_logging",
    "configure_debug_logging",
    "redact_text",
    "DEFAULT_DEBUG_LOG_LIMIT",
    "DEFAULT_DEBUG_LOG_DIR",
    "DEBUG_LOG_DIR_ENV",
]
