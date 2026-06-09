"""Tests for ``inspire account`` commands + storage helpers.

Every test uses ``monkeypatch`` to redirect ``Path.home()`` into a tmp
directory, so the real ``~/.inspire/`` is never touched. Storage helpers
resolve all paths lazily through ``Path.home()``, so this is sufficient.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from inspire.accounts import storage
from inspire.cli.commands.account import account


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return fake_home


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# --- storage unit tests ---------------------------------------------------


class TestValidateName:
    @pytest.mark.parametrize(
        "name",
        ["alice", "bob-1", "user_42", "a", "A1", "primary.prod", "x" * 64],
    )
    def test_accepts_good(self, name: str) -> None:
        assert storage.validate_name(name) == name.strip()

    @pytest.mark.parametrize(
        "name",
        ["", "  ", "-leading-dash", ".dot", "has space", "bad/slash", "x" * 65, "semi;colon"],
    )
    def test_rejects_bad(self, name: str) -> None:
        with pytest.raises(storage.AccountError):
            storage.validate_name(name)


class TestCreateListCurrent:
    def test_list_empty_by_default(self, home: Path) -> None:
        assert storage.list_accounts() == []
        assert storage.current_account() is None

    def test_create_then_list(self, home: Path) -> None:
        storage.create_account("alice", 'username = "alice"\n')
        assert storage.list_accounts() == ["alice"]
        assert (home / ".inspire" / "accounts" / "alice" / "config.toml").exists()

    def test_create_rejects_duplicate(self, home: Path) -> None:
        storage.create_account("alice", "x = 1\n")
        with pytest.raises(storage.AccountError):
            storage.create_account("alice", "y = 2\n")

    def test_create_overwrite(self, home: Path) -> None:
        storage.create_account("alice", "x = 1\n")
        storage.create_account("alice", "y = 2\n", overwrite=True)
        assert storage.account_config_path("alice").read_text() == "y = 2\n"

    def test_create_overwrite_clears_old_account_cache_files(self, home: Path) -> None:
        storage.create_account("alice", "x = 1\n")
        account_dir = storage.account_dir("alice")
        (account_dir / "web_session.json").write_text("{}")
        (account_dir / "bridges.json").write_text("{}")
        (account_dir / "rtunnel-proxy-state.json").write_text("{}")

        storage.create_account("alice", "y = 2\n", overwrite=True)

        assert storage.account_config_path("alice").read_text() == "y = 2\n"
        assert not (account_dir / "web_session.json").exists()
        assert not (account_dir / "bridges.json").exists()
        assert not (account_dir / "rtunnel-proxy-state.json").exists()

    def test_set_and_get_current(self, home: Path) -> None:
        storage.create_account("alice", "x = 1\n")
        storage.set_current_account("alice")
        assert storage.current_account() == "alice"

    def test_set_current_rejects_unknown(self, home: Path) -> None:
        with pytest.raises(storage.AccountError):
            storage.set_current_account("ghost")

    def test_remove_clears_current_if_active(self, home: Path) -> None:
        storage.create_account("alice", "x = 1\n")
        storage.create_account("bob", "x = 1\n")
        storage.set_current_account("alice")

        storage.remove_account("alice")
        assert storage.current_account() is None
        assert not storage.current_file().exists()
        assert storage.list_accounts() == ["bob"]

    def test_remove_keeps_current_if_different(self, home: Path) -> None:
        storage.create_account("alice", "x = 1\n")
        storage.create_account("bob", "x = 1\n")
        storage.set_current_account("alice")

        storage.remove_account("bob")
        assert storage.current_account() == "alice"

    def test_remove_unknown_raises(self, home: Path) -> None:
        with pytest.raises(storage.AccountError):
            storage.remove_account("ghost")

    def test_list_ignores_files_and_dirs_without_config(self, home: Path) -> None:
        accounts = home / ".inspire" / "accounts"
        accounts.mkdir(parents=True)
        (accounts / "stray.txt").write_text("junk")
        (accounts / "no-config-here").mkdir()
        storage.create_account("alice", "x = 1\n")
        assert storage.list_accounts() == ["alice"]


class TestRenameStorage:
    def test_rename_active_account_updates_current_and_preserves_files(
        self, home: Path
    ) -> None:
        storage.create_account("old", "x = 1\n")
        account_dir = storage.account_dir("old")
        (account_dir / "web_session.json").write_text('{"account": "old"}\n')
        (account_dir / "bridges.json").write_text('{"bridges": []}\n')
        storage.set_current_account("old")

        storage.rename_account("old", "new")

        assert storage.list_accounts() == ["new"]
        assert storage.current_account() == "new"
        assert not (home / ".inspire" / "accounts" / "old").exists()
        new_dir = home / ".inspire" / "accounts" / "new"
        assert (new_dir / "config.toml").read_text() == "x = 1\n"
        assert (new_dir / "web_session.json").read_text() == '{"account": "old"}\n'
        assert (new_dir / "bridges.json").read_text() == '{"bridges": []}\n'

    def test_rename_inactive_account_keeps_current(self, home: Path) -> None:
        storage.create_account("alice", "x = 1\n")
        storage.create_account("bob", "x = 1\n")
        storage.set_current_account("alice")

        storage.rename_account("bob", "charlie")

        assert storage.list_accounts() == ["alice", "charlie"]
        assert storage.current_account() == "alice"

    def test_rename_rejects_missing_source(self, home: Path) -> None:
        with pytest.raises(storage.AccountError, match="not found"):
            storage.rename_account("ghost", "new")

    def test_rename_rejects_existing_target(self, home: Path) -> None:
        storage.create_account("old", "x = 1\n")
        storage.create_account("new", "x = 1\n")

        with pytest.raises(storage.AccountError, match="already exists"):
            storage.rename_account("old", "new")

    def test_rename_rejects_same_name(self, home: Path) -> None:
        storage.create_account("alice", "x = 1\n")

        with pytest.raises(storage.AccountError, match="same"):
            storage.rename_account("alice", "alice")

    def test_rename_rewrites_notebook_target_cache(self, home: Path) -> None:
        storage.create_account("old", "x = 1\n")
        storage.create_account("other", "x = 1\n")
        cache = home / ".inspire" / "notebook-targets.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "version": 1,
                    "targets": {
                        "nb|workspace=CPU": {
                            "account": "old",
                            "bridge_name": "nb",
                            "updated_at": 1,
                        },
                        "other|workspace=CPU": {
                            "account": "other",
                            "bridge_name": "other",
                            "updated_at": 1,
                        },
                        "none|workspace=CPU": {
                            "account": None,
                            "bridge_name": "none",
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        storage.rename_account("old", "new")

        data = json.loads(cache.read_text(encoding="utf-8"))
        assert data["targets"]["nb|workspace=CPU"]["account"] == "new"
        assert data["targets"]["nb|workspace=CPU"]["updated_at"] >= 1
        assert data["targets"]["other|workspace=CPU"]["account"] == "other"
        assert data["targets"]["none|workspace=CPU"]["account"] is None


# --- CLI command tests ----------------------------------------------------


def _add(runner: CliRunner, *args: str, input_: str | None = None):
    return runner.invoke(account, ["add", *args], input=input_)


class TestAccountAddCommand:
    def test_interactive_walkthrough_accepts_all_defaults(
        self, home: Path, runner: CliRunner
    ) -> None:
        """Default path: five prompts (username / password x2 / base URL / proxy).
        Empty lines accept the shown defaults; proxy stays unset."""
        # username(accept default), password, confirm, base URL(default), proxy(empty)
        inputs = "\ns3cr3t\ns3cr3t\n\n\n"
        result = _add(runner, "alice", input_=inputs)
        assert result.exit_code == 0, result.output
        assert "Platform login username" in result.output
        assert "Confirm password" in result.output
        assert "Inspire base URL" in result.output
        assert "Proxy URL" in result.output
        assert "Created account" in result.output
        assert "Active account: alice" in result.output

        config = (home / ".inspire" / "accounts" / "alice" / "config.toml").read_text()
        assert 'username = "alice"' in config
        assert 'password = "s3cr3t"' in config
        assert 'base_url = "https://qz.sii.edu.cn"' in config
        assert "proxy" not in config
        assert (home / ".inspire" / "current").read_text().strip() == "alice"

    def test_interactive_collects_custom_values(
        self, home: Path, runner: CliRunner
    ) -> None:
        inputs = (
            "user-xyz\n"            # username override
            "s3cr3t\ns3cr3t\n"      # password + confirm
            "https://staging.x\n"   # custom base URL
            "http://127.0.0.1:7897\n"  # proxy
        )
        result = _add(runner, "alice", input_=inputs)
        assert result.exit_code == 0, result.output
        config = storage.account_config_path("alice").read_text()
        assert 'username = "user-xyz"' in config
        assert 'base_url = "https://staging.x"' in config
        assert 'playwright = "http://127.0.0.1:7897"' in config

    def test_interactive_password_mismatch_reprompts(
        self, home: Path, runner: CliRunner
    ) -> None:
        # Two mismatched passwords → Click re-asks; third/fourth succeed.
        inputs = "\nfirst\nsecond\nagain\nagain\n\n\n"
        result = _add(runner, "alice", input_=inputs)
        assert result.exit_code == 0, result.output
        assert "do not match" in result.output.lower() or "try again" in result.output.lower()
        config = storage.account_config_path("alice").read_text()
        assert 'password = "again"' in config

    def test_switches_active_when_user_confirms(
        self, home: Path, runner: CliRunner
    ) -> None:
        storage.create_account("alice", "x = 1\n")
        storage.set_current_account("alice")
        # Interactive: answer prompts + 'y' to the switch question.
        inputs = "\npw\npw\n\n\ny\n"
        result = _add(runner, "bob", input_=inputs)
        assert result.exit_code == 0, result.output
        assert "Switch to 'bob'" in result.output
        assert storage.current_account() == "bob"

    def test_keeps_active_when_user_declines(
        self, home: Path, runner: CliRunner
    ) -> None:
        storage.create_account("alice", "x = 1\n")
        storage.set_current_account("alice")
        inputs = "\npw\npw\n\n\nn\n"
        result = _add(runner, "bob", input_=inputs)
        assert result.exit_code == 0, result.output
        assert "Active account unchanged: alice" in result.output
        assert storage.current_account() == "alice"

    def test_non_interactive_requires_password(
        self, home: Path, runner: CliRunner
    ) -> None:
        result = _add(runner, "alice", "--non-interactive")
        assert result.exit_code != 0
        assert "--password is required" in result.output

    def test_non_interactive_with_all_flags(
        self, home: Path, runner: CliRunner
    ) -> None:
        result = _add(
            runner,
            "alice",
            "--non-interactive",
            "--password",
            "pw",
            "--proxy",
            "http://127.0.0.1:7897",
            "--username",
            "user-xyz",
            "--use",
        )
        assert result.exit_code == 0, result.output
        config = storage.account_config_path("alice").read_text()
        assert 'username = "user-xyz"' in config
        assert 'playwright = "http://127.0.0.1:7897"' in config
        assert storage.current_account() == "alice"

    def test_non_interactive_no_use_keeps_active(
        self, home: Path, runner: CliRunner
    ) -> None:
        # First account auto-activates even in non-interactive mode.
        _add(runner, "alice", "--non-interactive", "--password", "pw")
        assert storage.current_account() == "alice"

        # Second account with --no-use must not change active.
        result = _add(
            runner, "bob", "--non-interactive", "--password", "pw", "--no-use"
        )
        assert result.exit_code == 0, result.output
        assert storage.current_account() == "alice"

    def test_add_duplicate_fails(self, home: Path, runner: CliRunner) -> None:
        _add(runner, "alice", "--non-interactive", "--password", "pw")
        result = _add(runner, "alice", "--non-interactive", "--password", "pw")
        assert result.exit_code != 0
        assert "already exists" in result.output

    def test_add_existing_account_dir_fails_before_prompts(
        self, home: Path, runner: CliRunner
    ) -> None:
        orphan = home / ".inspire" / "accounts" / "alice"
        orphan.mkdir(parents=True)

        result = _add(runner, "alice", input_="\npw\npw\n\n\n")

        assert result.exit_code != 0
        assert "Account already exists: alice" in result.output
        assert "Platform login username" not in result.output

    def test_add_invalid_name(self, home: Path, runner: CliRunner) -> None:
        result = _add(runner, "bad name", "--non-interactive", "--password", "pw")
        assert result.exit_code != 0
        assert "Invalid account name" in result.output

    def test_password_with_special_chars_is_escaped(
        self, home: Path, runner: CliRunner
    ) -> None:
        result = _add(
            runner,
            "alice",
            "--non-interactive",
            "--password",
            'p"w\\x',
        )
        assert result.exit_code == 0, result.output
        config = storage.account_config_path("alice").read_text()
        # Round-trip through tomllib to confirm the escaped write parses back.
        try:
            import tomllib  # type: ignore[unresolved-import]
        except ModuleNotFoundError:  # pragma: no cover - py3.10
            import tomli as tomllib  # type: ignore[no-redef]
        parsed = tomllib.loads(config)
        assert parsed["auth"]["password"] == 'p"w\\x'


class TestAccountListCommand:
    def test_list_empty(self, home: Path, runner: CliRunner) -> None:
        result = runner.invoke(account, ["list"])
        assert result.exit_code == 0
        assert "No accounts configured" in result.output

    def test_list_marks_active(self, home: Path, runner: CliRunner) -> None:
        storage.create_account("alice", "x = 1\n")
        storage.create_account("bob", "x = 1\n")
        storage.set_current_account("bob")

        result = runner.invoke(account, ["list"])
        assert result.exit_code == 0
        lines = [line for line in result.output.splitlines() if line.strip()]
        assert lines == [" * bob", "   alice".replace("   ", "   ")] or lines == [
            "   alice",
            " * bob",
        ]
        # Sorted output, so alice comes first:
        assert lines == ["   alice", " * bob"]


class TestAccountUseCommand:
    def test_use_switches_active(self, home: Path, runner: CliRunner) -> None:
        storage.create_account("alice", "x = 1\n")
        storage.create_account("bob", "x = 1\n")
        storage.set_current_account("alice")

        result = runner.invoke(account, ["use", "bob"])
        assert result.exit_code == 0
        assert "Active account: bob" in result.output
        assert storage.current_account() == "bob"

    def test_use_unknown_fails(self, home: Path, runner: CliRunner) -> None:
        result = runner.invoke(account, ["use", "ghost"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_use_switches_layered_config_and_browser_api_cache(
        self, home: Path, runner: CliRunner
    ) -> None:
        from inspire.config import Config
        from inspire.platform.web.browser_api import core as browser_core

        storage.create_account(
            "alice",
            '[auth]\nusername = "alice"\npassword = "pw"\n'
            '[api]\nbase_url = "https://alice.example"\n',
        )
        storage.create_account(
            "bob",
            '[auth]\nusername = "bob"\npassword = "pw"\n'
            '[api]\nbase_url = "https://bob.example"\n',
        )
        storage.set_current_account("alice")
        browser_core.clear_browser_api_runtime_cache()
        cfg, _ = Config.from_files_and_env(require_credentials=False)
        assert cfg.username == "alice"
        assert browser_core._get_base_url() == "https://alice.example"

        result = runner.invoke(account, ["use", "bob"])

        assert result.exit_code == 0, result.output
        cfg, _ = Config.from_files_and_env(require_credentials=False)
        assert cfg.username == "bob"
        assert browser_core._get_base_url() == "https://bob.example"

    def test_use_clears_process_local_caches(self, home: Path, runner: CliRunner) -> None:
        from inspire.cli.utils.auth import AuthManager
        from inspire.platform.web import resources
        from inspire.platform.web.browser_api import core as browser_core

        storage.create_account("alice", "x = 1\n")
        storage.create_account("bob", "x = 1\n")
        storage.set_current_account("alice")

        AuthManager._token = "stale-token"  # type: ignore[attr-defined]
        AuthManager._expires_at = 9999999999  # type: ignore[attr-defined]
        AuthManager._api = object()  # type: ignore[assignment]
        AuthManager._cache_key = ("alice",)
        browser_core._cached_base_url = "https://alice.example"  # type: ignore[attr-defined]
        browser_core._cached_base_url_key = ("alice", None)  # type: ignore[attr-defined]
        browser_core._cached_browser_api_prefix = "/alice"  # type: ignore[attr-defined]
        browser_core._cached_browser_api_prefix_key = ("alice", None)  # type: ignore[attr-defined]
        resources._availability_cache = {"all": ["stale"]}  # type: ignore[attr-defined]
        resources._cache_time = 9999999999  # type: ignore[attr-defined]

        result = runner.invoke(account, ["use", "bob"])

        assert result.exit_code == 0, result.output
        assert AuthManager._api is None
        assert AuthManager._token is None
        assert AuthManager._cache_key is None
        assert browser_core._cached_base_url is None
        assert browser_core._cached_browser_api_prefix is None
        assert resources._availability_cache is None

    def test_use_preserves_switched_away_account_disk_caches(
        self, home: Path, runner: CliRunner
    ) -> None:
        storage.create_account("alice", "x = 1\n")
        storage.create_account("bob", "x = 1\n")
        storage.set_current_account("alice")
        alice_dir = storage.account_dir("alice")
        bob_dir = storage.account_dir("bob")
        for name in ("web_session.json", "bridges.json", "rtunnel-proxy-state.json"):
            (alice_dir / name).write_text(f"alice:{name}\n")
            (bob_dir / name).write_text(f"bob:{name}\n")

        result = runner.invoke(account, ["use", "bob"])

        assert result.exit_code == 0, result.output
        for name in ("web_session.json", "bridges.json", "rtunnel-proxy-state.json"):
            assert (alice_dir / name).read_text() == f"alice:{name}\n"
            assert (bob_dir / name).read_text() == f"bob:{name}\n"

    def test_token_api_getter_is_removed(self, home: Path) -> None:
        from inspire.cli.utils.auth import AuthManager, AuthenticationError
        from inspire.config import Config

        storage.create_account(
            "alice",
            '[auth]\nusername = "alice-user"\npassword = "alice-pw"\n'
            '[api]\nbase_url = "https://alice.example"\n',
        )
        storage.set_current_account("alice")
        cfg, _ = Config.from_files_and_env(require_credentials=True)

        with pytest.raises(AuthenticationError, match="Browser API helpers"):
            AuthManager.get_api(cfg)

    def test_rtunnel_state_cache_lives_under_active_account(
        self, home: Path
    ) -> None:
        from inspire.platform.web.browser_api import rtunnel as rtunnel_module

        storage.create_account("alice", "x = 1\n")
        storage.create_account("bob", "x = 1\n")

        storage.set_current_account("alice")
        rtunnel_module.save_rtunnel_proxy_state(
            notebook_id="nb-1",
            proxy_url="https://alice.example/proxy/31337/",
            port=31337,
            ssh_port=22222,
            base_url="https://qz.example",
            account=storage.current_account(),
            now_ts=100.0,
        )

        storage.set_current_account("bob")
        rtunnel_module.save_rtunnel_proxy_state(
            notebook_id="nb-1",
            proxy_url="https://bob.example/proxy/31337/",
            port=31337,
            ssh_port=22222,
            base_url="https://qz.example",
            account=storage.current_account(),
            now_ts=100.0,
        )

        alice_state = storage.account_dir("alice") / "rtunnel-proxy-state.json"
        bob_state = storage.account_dir("bob") / "rtunnel-proxy-state.json"
        assert alice_state.exists()
        assert bob_state.exists()
        assert "alice.example" in alice_state.read_text()
        assert "bob.example" in bob_state.read_text()

    def test_rtunnel_state_falls_back_for_non_account_login_name(
        self, home: Path
    ) -> None:
        from inspire.platform.web.browser_api import rtunnel as rtunnel_module

        state_file = rtunnel_module.get_rtunnel_state_file(
            account="user-1",
            cache_dir=None,
        )

        assert state_file == home / ".cache" / "inspire-skill" / (
            "rtunnel-proxy-state-user-1.json"
        )
        assert not (home / ".inspire" / "accounts" / "user-1").exists()


class TestAccountRenameCommand:
    def test_rename_active_account(self, home: Path, runner: CliRunner) -> None:
        storage.create_account(
            "old",
            '[auth]\nusername = "platform-user"\npassword = "pw"\n',
        )
        storage.set_current_account("old")

        result = runner.invoke(account, ["rename", "old", "new"])

        assert result.exit_code == 0, result.output
        assert "Renamed account: old -> new" in result.output
        assert "Active account: new" in result.output
        assert storage.current_account() == "new"
        assert storage.list_accounts() == ["new"]
        assert 'username = "platform-user"' in storage.account_config_path("new").read_text()

    def test_rename_inactive_account(self, home: Path, runner: CliRunner) -> None:
        storage.create_account("alice", "x = 1\n")
        storage.create_account("bob", "x = 1\n")
        storage.set_current_account("alice")

        result = runner.invoke(account, ["rename", "bob", "charlie"])

        assert result.exit_code == 0, result.output
        assert "Renamed account: bob -> charlie" in result.output
        assert "Active account:" not in result.output
        assert storage.current_account() == "alice"
        assert storage.list_accounts() == ["alice", "charlie"]

    def test_rename_unknown_fails(self, home: Path, runner: CliRunner) -> None:
        result = runner.invoke(account, ["rename", "ghost", "new"])

        assert result.exit_code != 0
        assert "not found" in result.output

    def test_rename_existing_target_fails(self, home: Path, runner: CliRunner) -> None:
        storage.create_account("old", "x = 1\n")
        storage.create_account("new", "x = 1\n")

        result = runner.invoke(account, ["rename", "old", "new"])

        assert result.exit_code != 0
        assert "already exists" in result.output


class TestAccountCurrentCommand:
    def test_current_prints_active(self, home: Path, runner: CliRunner) -> None:
        storage.create_account("alice", "x = 1\n")
        storage.set_current_account("alice")

        result = runner.invoke(account, ["current"])
        assert result.exit_code == 0
        assert result.output.strip() == "alice"

    def test_current_exits_1_when_no_active(self, home: Path, runner: CliRunner) -> None:
        result = runner.invoke(account, ["current"])
        assert result.exit_code == 1
        # Hint goes to stderr; Click's CliRunner merges by default, so check output.
        assert "No active account" in result.output


class TestAccountRemoveCommand:
    def test_remove_with_yes_succeeds(self, home: Path, runner: CliRunner) -> None:
        storage.create_account("alice", "x = 1\n")
        result = runner.invoke(account, ["remove", "alice", "--yes"])
        assert result.exit_code == 0
        assert storage.list_accounts() == []

    def test_remove_without_yes_requires_confirm(
        self, home: Path, runner: CliRunner
    ) -> None:
        storage.create_account("alice", "x = 1\n")
        result = runner.invoke(account, ["remove", "alice"], input="y\n")
        assert result.exit_code == 0
        assert storage.list_accounts() == []

    def test_remove_abort(self, home: Path, runner: CliRunner) -> None:
        storage.create_account("alice", "x = 1\n")
        result = runner.invoke(account, ["remove", "alice"], input="n\n")
        assert result.exit_code != 0
        assert storage.list_accounts() == ["alice"]

    def test_remove_unknown_fails(self, home: Path, runner: CliRunner) -> None:
        result = runner.invoke(account, ["remove", "ghost", "--yes"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_remove_active_clears_current(self, home: Path, runner: CliRunner) -> None:
        storage.create_account("alice", "x = 1\n")
        storage.set_current_account("alice")

        result = runner.invoke(account, ["remove", "alice", "--yes"])
        assert result.exit_code == 0
        assert storage.current_account() is None


# --- CLI wiring sanity ----------------------------------------------------


def test_account_group_registered_on_main_cli() -> None:
    from inspire.cli.main import main as cli_main

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--help"])
    assert result.exit_code == 0
    assert "account" in result.output


def test_account_group_help_lists_subcommands() -> None:
    from inspire.cli.main import main as cli_main

    runner = CliRunner()
    result = runner.invoke(cli_main, ["account", "--help"])
    assert result.exit_code == 0
    for sub in ("add", "list", "use", "current", "remove", "rename"):
        assert sub in result.output, f"missing subcommand in help: {sub}\n{result.output}"
