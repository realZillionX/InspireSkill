"""Tests for debug report logging setup and error-path reporting."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from click.testing import CliRunner

from inspire.cli.context import EXIT_GENERAL_ERROR
from inspire.cli.logging_setup import clear_debug_logging, configure_debug_logging, redact_text
from inspire.cli.main import main as cli_main
from inspire.config import Config, SOURCE_ENV


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


def test_debug_report_redacts_platform_handles(monkeypatch, tmp_path: Path) -> None:
    log_dir = tmp_path / "debug-logs"
    monkeypatch.setenv("INSPIRE_DEBUG_LOG_DIR", str(log_dir))

    report_path = configure_debug_logging(
        argv=[
            "inspire",
            "--debug",
            "job",
            "status",
            "job-1234abcd",
        ]
    )
    assert report_path is not None
    logging.getLogger("inspire.test").debug(
        "resolved job-1234abcd as 550e8400-e29b-41d4-a716-446655440000"
    )
    clear_debug_logging()

    content = Path(report_path).read_text(encoding="utf-8")
    assert "job-1234abcd" not in content
    assert "550e8400-e29b-41d4-a716-446655440000" not in content
    assert "<redacted>" in content


def test_debug_report_omits_absolute_paths_and_report_location(
    monkeypatch,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "debug-logs"
    monkeypatch.setenv("INSPIRE_DEBUG_LOG_DIR", str(log_dir))

    report_path = configure_debug_logging(argv=["inspire", "--debug"])
    assert report_path is not None
    logging.getLogger("inspire.test").exception(
        "failed at /Users/alice/private/project/run.py"
    )
    clear_debug_logging()

    content = Path(report_path).read_text(encoding="utf-8")
    assert "/Users/alice/private/project/run.py" not in content
    assert str(Path.cwd()) not in content
    assert str(report_path) not in content


def test_debug_report_redacts_separate_and_inline_secret_arguments(
    monkeypatch,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "debug-logs"
    monkeypatch.setenv("INSPIRE_DEBUG_LOG_DIR", str(log_dir))

    report_path = configure_debug_logging(
        argv=[
            "inspire",
            "account",
            "add",
            "primary",
            "--password",
            "plain-secret",
            "--api-key=inline-secret",
            "--authorization",
            "Bearer auth-secret",
            "--cookie",
            "session=cookie-secret",
            "--username=253108120116",
            "-u",
            "student-42",
        ]
    )
    logging.getLogger("inspire.test").debug(
        "username=253108120116 login_name=student-42 user_id=user-hidden"
    )
    clear_debug_logging()

    assert report_path is not None
    content = Path(report_path).read_text(encoding="utf-8")
    for secret in (
        "plain-secret",
        "inline-secret",
        "auth-secret",
        "cookie-secret",
        "253108120116",
        "student-42",
        "user-hidden",
    ):
        assert secret not in content
    assert content.count("<redacted>") >= 7


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


def test_debug_error_keeps_report_path_out_of_human_mode(
    monkeypatch, tmp_path: Path
) -> None:
    log_dir = tmp_path / "debug-logs"
    monkeypatch.setenv("INSPIRE_DEBUG_LOG_DIR", str(log_dir))

    missing = tmp_path / "missing-file.txt"
    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["--debug", "notebook", "scp", "any", str(missing), "/tmp/dst"]
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Local path not found" in result.output
    assert "Debug diagnostics were written locally." in result.output
    assert str(log_dir) not in result.output
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


def test_debug_does_not_expand_account_show_output(
    monkeypatch,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "debug-logs"
    monkeypatch.setenv("INSPIRE_DEBUG_LOG_DIR", str(log_dir))
    cfg = Config(
        username="alice",
        password="secret",
        base_url="https://inspire.example",
    )
    sources = {
        "username": SOURCE_ENV,
        "password": SOURCE_ENV,
        "base_url": SOURCE_ENV,
    }
    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, **_kwargs: (cfg, sources)),
    )
    monkeypatch.setattr(
        Config,
        "get_config_paths",
        classmethod(lambda cls: (tmp_path / "global.toml", tmp_path / "project.toml")),
    )

    runner = CliRunner()
    plain_human = runner.invoke(
        cli_main,
        ["--no-env-file", "account", "show"],
    )
    debug_human = runner.invoke(
        cli_main,
        ["--debug", "--no-env-file", "account", "show"],
    )
    plain_json = runner.invoke(
        cli_main,
        ["--json", "--no-env-file", "account", "show"],
    )
    debug_json = runner.invoke(
        cli_main,
        ["--debug", "--json", "--no-env-file", "account", "show"],
    )
    clear_debug_logging()

    assert plain_human.exit_code == 0, plain_human.output
    assert debug_human.exit_code == 0, debug_human.output
    assert debug_human.output == plain_human.output
    assert "Config file:" not in debug_human.output
    assert "Precedence:" not in debug_human.output

    assert plain_json.exit_code == 0, plain_json.output
    assert debug_json.exit_code == 0, debug_json.output
    assert json.loads(debug_json.output) == json.loads(plain_json.output)
    data = json.loads(debug_json.output)["data"]
    assert set(data) == {"account", "values", "effective_proxy"}
    assert data["values"]["INSPIRE_USERNAME"] == "<configured>"
    assert "alice" not in debug_json.output


def test_debug_does_not_expand_account_check_output(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from inspire.cli.commands.account import check as check_module

    log_dir = tmp_path / "debug-logs"
    monkeypatch.setenv("INSPIRE_DEBUG_LOG_DIR", str(log_dir))
    cfg = Config(
        username="alice",
        password="secret",
        base_url="https://inspire.example",
    )
    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, **_kwargs: (cfg, {"base_url": SOURCE_ENV})),
    )
    monkeypatch.setattr(
        Config,
        "get_config_paths",
        classmethod(lambda cls: (tmp_path / "global.toml", tmp_path / "project.toml")),
    )
    monkeypatch.setattr(check_module, "get_web_session", lambda: object())
    monkeypatch.setattr(
        check_module.browser_api_module,
        "get_current_user",
        lambda session=None: {"name": "alice"},
    )

    runner = CliRunner()
    plain_human = runner.invoke(
        cli_main,
        ["--no-env-file", "account", "check"],
    )
    debug_human = runner.invoke(
        cli_main,
        ["--debug", "--no-env-file", "account", "check"],
    )
    plain_json = runner.invoke(
        cli_main,
        ["--json", "--no-env-file", "account", "check"],
    )
    debug_json = runner.invoke(
        cli_main,
        ["--debug", "--json", "--no-env-file", "account", "check"],
    )
    clear_debug_logging()

    assert plain_human.exit_code == 0, plain_human.output
    assert debug_human.exit_code == 0, debug_human.output
    assert debug_human.output == plain_human.output
    assert "Endpoint:" not in debug_human.output
    assert "Config files:" not in debug_human.output

    assert plain_json.exit_code == 0, plain_json.output
    assert debug_json.exit_code == 0, debug_json.output
    assert json.loads(debug_json.output) == json.loads(plain_json.output)
    assert json.loads(debug_json.output)["data"] == {
        "configured": True,
        "authenticated": True,
    }
