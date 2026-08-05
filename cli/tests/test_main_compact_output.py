from __future__ import annotations

import importlib
import json
import sys

import pytest

from inspire.cli.context import EXIT_GENERAL_ERROR

main_module = importlib.import_module("inspire.cli.main")


def test_top_level_json_failure_emits_only_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> None:
        raise RuntimeError(
            "request failed for job-12345678-1234-1234-1234-123456789abc "
            "via https://internal.example/api at /inspire/private/session.json "
            "login_name=alice"
        )

    monkeypatch.setattr(main_module, "main", fail)
    monkeypatch.setattr(sys, "argv", ["inspire", "--json"])

    with pytest.raises(SystemExit) as exc_info:
        main_module.cli()

    assert exc_info.value.code == EXIT_GENERAL_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert json.loads(captured.err) == {
        "success": False,
        "error": {
            "type": "Error",
            "code": EXIT_GENERAL_ERROR,
            "message": (
                "request failed for <redacted> via <redacted> at <redacted> "
                "login_name=<redacted>"
            ),
        },
    }


def test_top_level_human_failure_redacts_internal_context(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> None:
        raise RuntimeError(
            "request https://internal.example/api failed at "
            "/inspire/private/session.json login_name=alice"
        )

    monkeypatch.setattr(main_module, "main", fail)
    monkeypatch.setattr(sys, "argv", ["inspire"])

    with pytest.raises(SystemExit) as exc_info:
        main_module.cli()

    assert exc_info.value.code == EXIT_GENERAL_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Error: request <redacted> failed at <redacted> "
        "login_name=<redacted>\n"
    )
    for sensitive in (
        "internal.example",
        "/inspire/private/session.json",
        "alice",
    ):
        assert sensitive not in captured.err
