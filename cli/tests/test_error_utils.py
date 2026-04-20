"""Unit tests for inspire.cli.utils.errors: emit_error and exit_with_error."""

from __future__ import annotations

import json

import pytest

from inspire.cli.context import EXIT_GENERAL_ERROR, EXIT_CONFIG_ERROR, Context
from inspire.cli.utils.errors import emit_error, exit_with_error


# ---------------------------------------------------------------------------
# emit_error
# ---------------------------------------------------------------------------


def test_emit_error_human_mode(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = Context()
    code = emit_error(ctx, "TestError", "something broke")

    assert code == EXIT_GENERAL_ERROR
    captured = capsys.readouterr()
    assert "Error:" in captured.err
    assert "something broke" in captured.err


def test_emit_error_human_mode_with_hint(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = Context()
    code = emit_error(ctx, "TestError", "bad config", hint="Check your TOML")

    assert code == EXIT_GENERAL_ERROR
    captured = capsys.readouterr()
    assert "Error:" in captured.err
    assert "bad config" in captured.err
    assert "Check your TOML" in captured.err


def test_emit_error_json_mode(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = Context()
    ctx.json_output = True
    code = emit_error(ctx, "TestError", "something broke")

    assert code == EXIT_GENERAL_ERROR
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["success"] is False
    assert payload["error"]["type"] == "TestError"
    assert payload["error"]["message"] == "something broke"


def test_emit_error_json_mode_with_hint(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = Context()
    ctx.json_output = True
    code = emit_error(ctx, "TestError", "bad config", hint="Check your TOML")

    assert code == EXIT_GENERAL_ERROR
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["error"]["hint"] == "Check your TOML"


def test_emit_error_custom_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = Context()
    code = emit_error(ctx, "ConfigError", "missing field", EXIT_CONFIG_ERROR)

    assert code == EXIT_CONFIG_ERROR


def test_emit_error_does_not_exit(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = Context()
    # Should return normally, not raise SystemExit
    code = emit_error(ctx, "TestError", "no exit")
    assert isinstance(code, int)


def test_emit_error_default_exit_code() -> None:
    """Default exit_code parameter is EXIT_GENERAL_ERROR."""
    ctx = Context()
    code = emit_error(ctx, "TestError", "msg")
    assert code == EXIT_GENERAL_ERROR


# ---------------------------------------------------------------------------
# exit_with_error
# ---------------------------------------------------------------------------


def test_exit_with_error_raises_system_exit() -> None:
    ctx = Context()
    with pytest.raises(SystemExit) as exc_info:
        exit_with_error(ctx, "FatalError", "boom")

    assert exc_info.value.code == EXIT_GENERAL_ERROR


def test_exit_with_error_custom_code() -> None:
    ctx = Context()
    with pytest.raises(SystemExit) as exc_info:
        exit_with_error(ctx, "ConfigError", "missing field", EXIT_CONFIG_ERROR)

    assert exc_info.value.code == EXIT_CONFIG_ERROR


def test_exit_with_error_human_output(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = Context()
    with pytest.raises(SystemExit):
        exit_with_error(ctx, "FatalError", "boom", hint="try again")

    captured = capsys.readouterr()
    assert "Error:" in captured.err
    assert "boom" in captured.err
    assert "try again" in captured.err


def test_exit_with_error_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = Context()
    ctx.json_output = True
    with pytest.raises(SystemExit):
        exit_with_error(ctx, "FatalError", "boom")

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["success"] is False
    assert payload["error"]["type"] == "FatalError"
