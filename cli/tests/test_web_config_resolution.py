"""Tests for web-facing config resolution helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from inspire.cli.utils.notebook_cli import get_base_url
from inspire.platform.web.browser_api.notebooks import list_notebook_compute_groups


def test_notebook_cli_base_url_reads_account_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Account-scoped ``[api].base_url`` is used when the environment is unset."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    account_dir = fake_home / ".inspire" / "accounts" / "alice"
    account_dir.mkdir(parents=True)
    (account_dir / "config.toml").write_text(
        '[auth]\nusername = "alice"\npassword = "pw"\n'
        '[api]\nbase_url = "https://account.example"\n',
        encoding="utf-8",
    )
    (fake_home / ".inspire" / "current").write_text("alice\n", encoding="utf-8")

    project_dir = tmp_path / "repo" / ".inspire"
    project_dir.mkdir(parents=True)
    (project_dir / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.delenv("INSPIRE_BASE_URL", raising=False)

    assert get_base_url() == "https://account.example"


def test_notebook_compute_groups_ignore_legacy_config_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    account_dir = fake_home / ".inspire" / "accounts" / "alice"
    account_dir.mkdir(parents=True)
    (account_dir / "config.toml").write_text("", encoding="utf-8")
    (fake_home / ".inspire" / "current").write_text("alice\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    (account_dir / "config.toml").write_text(
        """
[[compute_groups]]
name = "H200 A"
id = "lcg-test-1"
gpu_type = "H200"
""",
        encoding="utf-8",
    )
    from inspire.platform.web.browser_api.availability import api as availability_api

    monkeypatch.setattr(availability_api, "list_compute_groups", lambda **_: [])

    groups = list_notebook_compute_groups(
        workspace_id="workspace-test",
        session=object(),  # type: ignore[arg-type]
    )

    assert groups == []
