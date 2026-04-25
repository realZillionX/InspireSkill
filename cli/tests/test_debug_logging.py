"""Tests for debug report logging setup and error-path reporting."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from click.testing import CliRunner

from inspire.cli.context import EXIT_GENERAL_ERROR
from inspire.cli.logging_setup import clear_debug_logging, configure_debug_logging, redact_text
from inspire.cli.main import main as cli_main


def test_redact_text_masks_common_sensitive_patterns() -> None:
    raw = (
        "Authorization: Bearer abc123\n"
        "token=abc123&x=1\n"
        '{"password":"s3cr3t","api_key":"xyz"}\n'
        "/jupyter/nb-1/mytoken/proxy/31337"
    )

    redacted = redact_text(raw)
    assert "abc123" not in redacted
    assert "s3cr3t" not in redacted
    assert "xyz" not in redacted
    assert "<redacted>" in redacted


def test_configure_debug_logging_creates_report_and_prunes(monkeypatch, tmp_path: Path) -> None:
    log_dir = tmp_path / "debug-logs"
    monkeypatch.setenv("INSPIRE_DEBUG_LOG_DIR", str(log_dir))

    log_dir.mkdir(parents=True, exist_ok=True)
    for idx in range(6):
        old_file = log_dir / f"inspire-debug-20250101-00000{idx}-1.log"
        old_file.write_text("old")

    report_path = configure_debug_logging(argv=["inspire", "--debug"], keep_logs=3)
    assert report_path is not None

    report = Path(report_path)
    assert report.exists()
    content = report.read_text(encoding="utf-8")
    assert "Debug session started" in content
    assert "argv=['inspire', '--debug']" in content

    remaining = sorted(log_dir.glob("inspire-debug-*.log"))
    assert len(remaining) <= 3


def test_configure_debug_logging_uses_unique_report_paths(monkeypatch, tmp_path: Path) -> None:
    log_dir = tmp_path / "debug-logs"
    monkeypatch.setenv("INSPIRE_DEBUG_LOG_DIR", str(log_dir))

    first = configure_debug_logging(argv=["inspire", "--debug"])
    clear_debug_logging()
    second = configure_debug_logging(argv=["inspire", "--debug"])
    clear_debug_logging()

    assert first is not None and second is not None
    assert first != second
    assert Path(first).exists()
    assert Path(second).exists()


def test_clear_debug_logging_restores_logger_state(monkeypatch, tmp_path: Path) -> None:
    log_dir = tmp_path / "debug-logs"
    monkeypatch.setenv("INSPIRE_DEBUG_LOG_DIR", str(log_dir))

    inspire_logger = logging.getLogger("inspire")
    original_level = inspire_logger.level
    original_propagate = inspire_logger.propagate

    clear_debug_logging()
    inspire_logger.setLevel(logging.WARNING)
    inspire_logger.propagate = True

    configure_debug_logging(argv=["inspire", "--debug"])
    assert inspire_logger.level == logging.DEBUG
    assert inspire_logger.propagate is False

    clear_debug_logging()
    assert inspire_logger.level == logging.WARNING
    assert inspire_logger.propagate is True

    inspire_logger.setLevel(original_level)
    inspire_logger.propagate = original_propagate


def test_debug_error_prints_report_path_in_human_mode(monkeypatch, tmp_path: Path) -> None:
    log_dir = tmp_path / "debug-logs"
    monkeypatch.setenv("INSPIRE_DEBUG_LOG_DIR", str(log_dir))

    missing = tmp_path / "missing-file.txt"
    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["--debug", "notebook", "scp", "any", str(missing), "/tmp/dst"]
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Local path not found" in result.output
    assert "Debug report:" in result.output
    assert len(list(log_dir.glob("inspire-debug-*.log"))) == 1


def test_debug_error_keeps_json_output_clean(monkeypatch, tmp_path: Path) -> None:
    log_dir = tmp_path / "debug-logs"
    monkeypatch.setenv("INSPIRE_DEBUG_LOG_DIR", str(log_dir))

    missing = tmp_path / "missing-file.txt"
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--debug", "--json", "notebook", "scp", "any", str(missing), "/tmp/dst"],
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert "Debug report:" not in result.output
    assert len(list(log_dir.glob("inspire-debug-*.log"))) == 1
