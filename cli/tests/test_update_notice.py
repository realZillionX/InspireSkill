from __future__ import annotations

from inspire.cli.utils import update_notice


def test_update_notice_is_silent_by_default(
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("INSPIRE_SKIP_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("INSPIRE_SHOW_UPDATE_NOTICE", raising=False)
    monkeypatch.setattr(
        update_notice,
        "_read_cache",
        lambda: {"current": "1.0.0", "latest": "2.0.0"},
    )

    update_notice.maybe_notify_update()

    assert capsys.readouterr().err == ""


def test_update_notice_remains_available_as_explicit_opt_in(
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("INSPIRE_SKIP_UPDATE_CHECK", raising=False)
    monkeypatch.setenv("INSPIRE_SHOW_UPDATE_NOTICE", "1")
    monkeypatch.setattr(
        update_notice,
        "_read_cache",
        lambda: {"current": "1.0.0", "latest": "2.0.0"},
    )
    monkeypatch.setattr(update_notice, "__version__", "1.0.0")

    update_notice.maybe_notify_update()

    error = capsys.readouterr().err
    assert "v2.0.0 available" in error
    assert "inspire update" in error
