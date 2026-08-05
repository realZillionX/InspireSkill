"""Options a newly installed CLI must keep accepting for older callers.

Both surfaces here are driven by artifacts we do not control at upgrade
time: the previously installed CLI (which spawns the new one) and
``~/.ssh/config`` entries written by an older ``ssh-config`` run. Dropping
either option turns a routine upgrade into a broken one, and the failure
surfaces far from this repo.
"""

from __future__ import annotations

import importlib

import pytest
from click.testing import CliRunner

from inspire.cli.main import main as cli_main

update_module = importlib.import_module("inspire.cli.commands.update")


def test_post_update_accepts_previous_version_from_older_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLIs at or below v6.2.0 always pass ``--previous-version``.

    They run ``<new inspire> _post-update --previous-version X
    --expected-version Y`` right after installing, then ``sys.exit(1)`` if it
    fails — skipping the skill refresh and runtime setup it was meant to do.
    """
    seen: dict[str, object] = {}

    def _tasks(*, expected_version: str, cli_only: bool, silent: bool) -> bool:
        seen.update(
            expected_version=expected_version, cli_only=cli_only, silent=silent
        )
        return True

    monkeypatch.setattr(update_module, "_run_post_update_tasks", _tasks)

    result = CliRunner().invoke(
        cli_main,
        [
            "_post-update",
            "--previous-version",
            "6.1.4",
            "--expected-version",
            "6.3.0",
            "--silent",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == {
        "expected_version": "6.3.0",
        "cli_only": False,
        "silent": True,
    }


def test_ssh_proxy_accepts_quiet_from_older_ssh_config_entries() -> None:
    """``ssh-config`` at or below v6.2.0 wrote ``--quiet`` into ProxyCommand.

    Those lines live in the user's ``~/.ssh/config`` and are never rewritten,
    so rejecting the flag breaks ``ssh <host>`` for every host configured
    before the upgrade. The command still fails here for lack of a resolvable
    notebook; what matters is that argv parses.
    """
    result = CliRunner().invoke(
        cli_main,
        ["notebook", "ssh-proxy", "demo-box", "--port", "22222", "--quiet"],
    )

    assert "No such option" not in result.output
    assert result.exit_code != 2, result.output
