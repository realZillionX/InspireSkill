from __future__ import annotations

import http.client
import urllib.error
from unittest.mock import patch

from click.testing import CliRunner

from inspire.cli.main import main
from inspire.cli.utils import update_notice


def _stub_cache(monkeypatch, latest: str = "2.0.0", current: str = "1.0.0") -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("INSPIRE_SKIP_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(
        update_notice,
        "_read_cache",
        lambda: {"current": current, "latest": latest},
    )
    monkeypatch.setattr(update_notice, "__version__", current)


def test_update_notice_fires_by_default(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """A newer version must be announced without anyone opting in.

    This used to require ``INSPIRE_SHOW_UPDATE_NOTICE=1``, which meant nobody
    was ever told a new release existed.
    """
    _stub_cache(monkeypatch)

    update_notice.maybe_notify_update()

    error = capsys.readouterr().err
    assert "v2.0.0 available" in error
    assert "inspire update" in error


def test_update_notice_goes_to_stderr_so_stdout_stays_clean(
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    _stub_cache(monkeypatch)

    update_notice.maybe_notify_update()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "v2.0.0 available" in captured.err


def test_update_notice_is_silenced_by_the_skip_env(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    _stub_cache(monkeypatch)
    monkeypatch.setenv("INSPIRE_SKIP_UPDATE_CHECK", "1")

    update_notice.maybe_notify_update()

    assert capsys.readouterr().err == ""


def test_update_notice_stays_quiet_when_already_current(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    _stub_cache(monkeypatch, latest="1.0.0", current="1.0.0")

    update_notice.maybe_notify_update()

    assert capsys.readouterr().err == ""


def test_json_output_still_suppresses_the_notice() -> None:
    """`--json` must stay a single JSON document, so the notice is skipped there."""
    with (
        patch("inspire.cli.main.maybe_notify_update") as notify,
        patch("inspire.cli.main.maybe_spawn_check"),
    ):
        CliRunner().invoke(main, ["--json", "cache", "status"])

    notify.assert_not_called()


def test_plain_output_reaches_the_notice_hook() -> None:
    """Counterpart to the `--json` case: the hook is wired up on the normal path."""
    with (
        patch("inspire.cli.main.maybe_notify_update") as notify,
        patch("inspire.cli.main.maybe_spawn_check"),
    ):
        CliRunner().invoke(main, ["cache", "status"])

    notify.assert_called_once()


def _invoke_check(installed: str, latest: str):  # type: ignore[no-untyped-def]
    """Run `inspire update --check` with the platform and install state stubbed."""
    with (
        patch("inspire.cli.commands.update.run_check") as run_check,
        patch("inspire.cli.commands.update._read_inspire_version") as read_version,
        patch("inspire.cli.commands.update._uv_tool_info", return_value=None),
        patch("inspire.cli.commands.update._audit_installed_skills", return_value=True),
    ):
        run_check.return_value = {
            "current": installed,
            "latest": latest,
            "checked_at": "2026-01-01T00:00:00Z",
            "source": "test",
        }
        read_version.return_value = ("/usr/local/bin/inspire", installed, "")
        return CliRunner().invoke(main, ["update", "--check"])


def test_update_check_reports_an_available_update_instead_of_failing() -> None:
    """A newer published version is the answer, not a failed check.

    Regression: the check path fed the latest published version into
    `_audit_update_state`, whose version comparison is a post-upgrade verifier.
    Being on an older release therefore failed the audit, so `--check` exited 1
    with `check failed` in exactly the case it exists to report — and the daily
    launchd agent, which runs `--check --silent`, exited 1 without a word.
    """
    result = _invoke_check(installed="7.1.0", latest="7.1.1")

    assert result.exit_code == 0, result.output
    assert "v7.1.0 → v7.1.1" in result.output
    assert "failed" not in result.output


def test_update_check_still_reports_up_to_date() -> None:
    result = _invoke_check(installed="7.1.1", latest="7.1.1")

    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


def test_update_check_still_fails_when_the_install_is_broken() -> None:
    """Dropping the version comparison must not stop the audit catching a bad install."""
    with (
        patch("inspire.cli.commands.update.run_check") as run_check,
        patch(
            "inspire.cli.commands.update._read_inspire_version",
            return_value=(None, None, "not on PATH"),
        ),
        patch("inspire.cli.commands.update._uv_tool_info", return_value=None),
        patch("inspire.cli.commands.update._audit_installed_skills", return_value=True),
    ):
        run_check.return_value = {
            "current": "7.1.0",
            "latest": "7.1.1",
            "checked_at": "2026-01-01T00:00:00Z",
            "source": "test",
        }
        result = CliRunner().invoke(main, ["update", "--check"])

    assert result.exit_code == 1
    assert "failed" in result.output


def test_truncated_pypi_response_falls_back_instead_of_raising(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`IncompleteRead` is an HTTPException, not an OSError.

    Regression: it escaped the except clause, so a truncated PyPI response took
    the whole check down with a traceback (seen in the launchd agent's log)
    instead of falling through to the GitHub `pyproject.toml` fallback.
    """
    calls: list[str] = []

    class _Resp:
        def __init__(self, payload: str | None) -> None:
            self._payload = payload

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def read(self) -> bytes:
            if self._payload is None:
                raise http.client.IncompleteRead(b"partial", 71112)
            return self._payload.encode()

    def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
        url = req.full_url
        calls.append(url)
        if url == update_notice.PYPI_JSON_URL:
            return _Resp(None)
        return _Resp('version = "9.9.9"\n')

    monkeypatch.setattr(update_notice.urllib.request, "urlopen", fake_urlopen)

    version, source = update_notice.fetch_latest_version_info()

    assert version == "9.9.9"
    assert source == update_notice.RAW_PYPROJECT_URL
    assert calls == [update_notice.PYPI_JSON_URL, update_notice.RAW_PYPROJECT_URL]


def test_malformed_pypi_json_still_falls_back(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class _Resp:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def read(self) -> bytes:
            return b"{not json"

    def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
        if req.full_url == update_notice.PYPI_JSON_URL:
            return _Resp()
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(update_notice.urllib.request, "urlopen", fake_urlopen)

    version, source = update_notice.fetch_latest_version_info()

    assert version is None
    assert source == update_notice.RAW_PYPROJECT_URL
