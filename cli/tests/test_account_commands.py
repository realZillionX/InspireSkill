"""Tests for ``inspire account`` commands + storage helpers.

Every test uses ``monkeypatch`` to redirect ``Path.home()`` into a tmp
directory, so the real ``~/.inspire/`` is never touched. Storage helpers
resolve all paths lazily through ``Path.home()``, so this is sufficient.
"""

from __future__ import annotations

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
        assert 'proxy = "http://127.0.0.1:7897"' in config

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
        assert 'proxy = "http://127.0.0.1:7897"' in config
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
        assert parsed["password"] == 'p"w\\x'


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
    for sub in ("add", "list", "use", "current", "remove"):
        assert sub in result.output, f"missing subcommand in help: {sub}\n{result.output}"
