"""Tests for ``inspire account migrate`` and the underlying migration module.

Every test redirects ``Path.home()`` into a tmp directory so the real
``~/.inspire/``, ``~/.config/inspire/``, and ``~/.cache/inspire-skill/``
are never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

try:
    import tomllib  # type: ignore[unresolved-import]
except ModuleNotFoundError:  # pragma: no cover - py3.10
    import tomli as tomllib  # type: ignore[no-redef]

from inspire.accounts import migration, storage
from inspire.cli.commands.account import account


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake)
    # Clear any env overrides that could push the legacy path elsewhere.
    monkeypatch.delenv("INSPIRE_GLOBAL_CONFIG_PATH", raising=False)
    return fake


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------- helpers -------------------------------------------------------


def _write_legacy_global(home: Path, body: str) -> Path:
    path = home / ".config" / "inspire" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _write_legacy_bridges(home: Path, user: str, body: str = '{"bridges": []}') -> Path:
    path = home / ".inspire" / f"bridges-{user}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _write_legacy_session(home: Path, user: str, body: str = '{"created_at": 0}') -> Path:
    path = home / ".cache" / "inspire-skill" / f"web_session-{user}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


# ---------- discovery / plan building -------------------------------------


class TestBuildPlan:
    def test_empty_host_produces_empty_plan(self, home: Path) -> None:
        plan = migration.build_plan()
        assert plan.is_empty
        assert plan.accounts == {}
        assert plan.active_account is None

    def test_single_legacy_account_with_all_artefacts(self, home: Path) -> None:
        _write_legacy_global(
            home,
            '[auth]\nusername = "alice"\n\n'
            '[api]\nbase_url = "https://alice.example.com"\ntimeout = 42\n\n'
            '[accounts."alice"]\npassword = "alice-pw"\n',
        )
        _write_legacy_bridges(home, "alice")
        _write_legacy_session(home, "alice")

        plan = migration.build_plan()

        assert "alice" in plan.accounts
        acct = plan.accounts["alice"]
        assert acct.legacy_name == "alice"
        assert acct.bridges_source is not None
        assert acct.web_session_source is not None

        parsed = tomllib.loads(acct.config_toml)
        assert parsed["auth"]["username"] == "alice"
        assert parsed["auth"]["password"] == "alice-pw"
        assert parsed["api"]["base_url"] == "https://alice.example.com"
        assert parsed["api"]["timeout"] == 42
        # [accounts] and [context] must not leak into the new file.
        assert "accounts" not in parsed
        assert "context" not in parsed

        assert plan.active_account == "alice"

    def test_multiple_accounts_each_get_their_own_plan(self, home: Path) -> None:
        _write_legacy_global(
            home,
            '[api]\nbase_url = "https://shared.example.com"\n\n'
            '[accounts."alice"]\npassword = "alice-pw"\n\n'
            '[accounts."bob"]\npassword = "bob-pw"\n',
        )
        _write_legacy_bridges(home, "alice")
        _write_legacy_bridges(home, "bob")

        plan = migration.build_plan()

        assert set(plan.accounts.keys()) == {"alice", "bob"}
        for name in ("alice", "bob"):
            parsed = tomllib.loads(plan.accounts[name].config_toml)
            assert parsed["auth"]["username"] == name
            assert parsed["auth"]["password"] == f"{name}-pw"
            assert parsed["api"]["base_url"] == "https://shared.example.com"

    def test_active_account_from_context_section(self, home: Path) -> None:
        _write_legacy_global(
            home,
            '[context]\naccount = "bob"\n\n'
            '[accounts."alice"]\npassword = "a"\n\n'
            '[accounts."bob"]\npassword = "b"\n',
        )
        plan = migration.build_plan()
        assert plan.active_account == "bob"

    def test_active_account_from_top_username_when_no_context(self, home: Path) -> None:
        _write_legacy_global(
            home,
            '[auth]\nusername = "alice"\n\n'
            '[accounts."alice"]\npassword = "a"\n\n'
            '[accounts."bob"]\npassword = "b"\n',
        )
        plan = migration.build_plan()
        assert plan.active_account == "alice"

    def test_active_account_single_account_fallback(self, home: Path) -> None:
        _write_legacy_bridges(home, "solo")
        plan = migration.build_plan()
        assert plan.active_account == "solo"

    def test_account_overrides_merge_workspace_aliases(self, home: Path) -> None:
        _write_legacy_global(
            home,
            '[workspaces]\ncpu = "ws-cpu-global"\n\n'
            '[accounts."alice"]\npassword = "pw"\n\n'
            '[accounts."alice".workspaces]\ngpu = "ws-gpu-alice"\n',
        )
        plan = migration.build_plan()
        parsed = tomllib.loads(plan.accounts["alice"].config_toml)
        assert parsed["workspaces"] == {"cpu": "ws-cpu-global", "gpu": "ws-gpu-alice"}

    def test_discovery_from_bridges_file_alone(self, home: Path) -> None:
        """A bridges-<name>.json file with no matching TOML entry still
        becomes an account (minimal config, username = legacy name)."""
        _write_legacy_bridges(home, "ghost")
        plan = migration.build_plan()
        assert "ghost" in plan.accounts
        parsed = tomllib.loads(plan.accounts["ghost"].config_toml)
        assert parsed.get("auth", {}).get("username") == "ghost"

    def test_legacy_paths_section_is_dropped_and_reported(self, home: Path) -> None:
        """[paths].target_dir is per-repo and cannot survive migration — the
        plan should strip it and surface the dropped value so the user knows
        to rebuild it with 'inspire init --discover' inside each repo."""
        _write_legacy_global(
            home,
            '[auth]\nusername = "alice"\n\n'
            '[accounts."alice"]\npassword = "pw"\n\n'
            '[paths]\ntarget_dir = "/inspire/ssd/project/foo/alice/work"\n',
        )

        plan = migration.build_plan()
        assert plan.dropped_target_dir == "/inspire/ssd/project/foo/alice/work"

        parsed = tomllib.loads(plan.accounts["alice"].config_toml)
        assert "paths" not in parsed  # scrubbed

        summary = "\n".join(migration.describe_plan(plan))
        assert "target_dir" in summary
        assert "init --discover" in summary

    def test_migration_result_loads_cleanly(self, home: Path) -> None:
        """End-to-end: after migrate runs, Config.from_files_and_env must not
        blow up reading the new account config (no stray [paths] left over)."""
        _write_legacy_global(
            home,
            '[auth]\nusername = "alice"\n\n'
            '[accounts."alice"]\npassword = "pw"\n\n'
            '[paths]\ntarget_dir = "/inspire/ssd/project/foo/alice/work"\n',
        )
        plan = migration.build_plan()
        migration.execute_plan(plan)

        from inspire.config import Config
        cfg, _ = Config.from_files_and_env(require_credentials=False)
        assert cfg.username == "alice"
        # target_dir did not survive, as intended.
        assert cfg.target_dir is None


# ---------- execution -----------------------------------------------------


class TestExecutePlan:
    def test_creates_account_dirs_and_moves_files(self, home: Path) -> None:
        _write_legacy_global(
            home,
            '[accounts."alice"]\npassword = "alice-pw"\n',
        )
        bridges = _write_legacy_bridges(home, "alice", '{"bridges": [{"name": "x", "proxy_url": "u"}]}')
        session = _write_legacy_session(home, "alice")

        plan = migration.build_plan()
        backup = migration.execute_plan(plan)

        acct_dir = home / ".inspire" / "accounts" / "alice"
        assert (acct_dir / "config.toml").exists()
        assert (acct_dir / "bridges.json").exists()
        assert (acct_dir / "web_session.json").exists()
        # Originals gone (moved into new dir or unlinked).
        assert not bridges.exists()
        assert not session.exists()
        # Backup dir exists and contains copies of the originals.
        assert backup.exists()
        assert (backup / "config.toml").exists()

    def test_sets_active_account(self, home: Path) -> None:
        _write_legacy_bridges(home, "solo")
        plan = migration.build_plan()
        migration.execute_plan(plan)
        assert storage.current_account() == "solo"

    def test_refuses_when_target_account_already_exists(self, home: Path) -> None:
        _write_legacy_global(home, '[accounts."alice"]\npassword = "pw"\n')
        storage.create_account("alice", "x = 1\n")

        plan = migration.build_plan()
        with pytest.raises(migration.MigrationConflictError):
            migration.execute_plan(plan)

    def test_removes_unscoped_legacy_files(self, home: Path) -> None:
        _write_legacy_bridges(home, "alice")
        unscoped_bridges = home / ".inspire" / "bridges.json"
        unscoped_bridges.parent.mkdir(parents=True, exist_ok=True)
        unscoped_bridges.write_text('{"bridges": []}')
        unscoped_session = home / ".cache" / "inspire-skill" / "web_session.json"
        unscoped_session.parent.mkdir(parents=True, exist_ok=True)
        unscoped_session.write_text("{}")

        plan = migration.build_plan()
        backup = migration.execute_plan(plan)

        assert not unscoped_bridges.exists()
        assert not unscoped_session.exists()
        assert (backup / "bridges.json").exists()
        assert (backup / "web_session.json").exists()


# ---------- CLI -----------------------------------------------------------


class TestMigrateCommand:
    def test_dry_run_touches_nothing(self, home: Path, runner: CliRunner) -> None:
        _write_legacy_global(home, '[accounts."alice"]\npassword = "pw"\n')
        bridges = _write_legacy_bridges(home, "alice")

        result = runner.invoke(account, ["migrate", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output.lower()
        assert bridges.exists()  # still there
        assert not (home / ".inspire" / "accounts" / "alice").exists()

    def test_empty_state_exits_cleanly(self, home: Path, runner: CliRunner) -> None:
        result = runner.invoke(account, ["migrate"])
        assert result.exit_code == 0, result.output
        assert "Nothing to migrate" in result.output

    def test_yes_skips_confirm_and_applies(self, home: Path, runner: CliRunner) -> None:
        _write_legacy_global(home, '[accounts."alice"]\npassword = "pw"\n')
        _write_legacy_bridges(home, "alice")

        result = runner.invoke(account, ["migrate", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Migration complete" in result.output
        assert storage.current_account() == "alice"

    def test_abort_at_confirm_does_not_modify_state(
        self, home: Path, runner: CliRunner
    ) -> None:
        _write_legacy_global(home, '[accounts."alice"]\npassword = "pw"\n')
        bridges = _write_legacy_bridges(home, "alice")

        result = runner.invoke(account, ["migrate"], input="n\n")
        assert result.exit_code != 0  # click aborts
        assert bridges.exists()
        assert storage.list_accounts() == []

    def test_conflict_yields_clickexception(
        self, home: Path, runner: CliRunner
    ) -> None:
        _write_legacy_global(home, '[accounts."alice"]\npassword = "pw"\n')
        _write_legacy_bridges(home, "alice")
        storage.create_account("alice", "x = 1\n")

        result = runner.invoke(account, ["migrate", "--yes"])
        assert result.exit_code != 0
        assert "already exist" in result.output


# ---------- TOML dumper sanity -------------------------------------------


class TestTomlDumper:
    def test_roundtrip_flat_and_nested(self) -> None:
        data = {
            "username": "alice",
            "api": {"base_url": "https://x.y", "timeout": 42, "skip_ssl_verify": True},
            "workspaces": {"cpu": "ws-1", "has space": "ws-2"},
            "compute_groups": [
                {"id": "cg-1", "name": "first"},
                {"id": "cg-2", "name": "second"},
            ],
        }
        text = migration._dump_toml(data)
        parsed = tomllib.loads(text)
        assert parsed["username"] == "alice"
        assert parsed["api"]["base_url"] == "https://x.y"
        assert parsed["api"]["timeout"] == 42
        assert parsed["api"]["skip_ssl_verify"] is True
        assert parsed["workspaces"]["cpu"] == "ws-1"
        assert parsed["workspaces"]["has space"] == "ws-2"
        assert [cg["id"] for cg in parsed["compute_groups"]] == ["cg-1", "cg-2"]

    def test_special_chars_in_strings(self) -> None:
        data = {"password": 'p"w\\x'}
        text = migration._dump_toml(data)
        parsed = tomllib.loads(text)
        assert parsed["password"] == 'p"w\\x'
