import importlib
from types import SimpleNamespace

from click.testing import CliRunner

from inspire.cli.main import main as cli_main


def test_cli_help_includes_top_level_groups() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--help"])
    assert result.exit_code == 0
    for group in ("job", "notebook", "image", "resources", "hpc", "run"):
        assert group in result.output, f"missing: {group}\n{result.output}"
    # bridge / tunnel were merged into notebook
    assert "bridge" not in result.output
    assert "tunnel" not in result.output


def test_job_help_includes_key_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "--help"])
    assert result.exit_code == 0
    assert "create" in result.output
    assert "logs" in result.output


def test_notebook_help_includes_key_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "--help"])
    assert result.exit_code == 0
    for sub in (
        "list", "status", "ssh", "exec", "scp", "shell",
        "connections", "refresh", "forget", "test",
    ):
        assert sub in result.output, f"missing: {sub}\n{result.output}"
    # set-default and the --save-as alias concept are gone
    assert "set-default" not in result.output


def test_notebook_ssh_help_mentions_bootstrap_no_save_as() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "ssh", "--help"])
    assert result.exit_code == 0
    assert "bootstrap" in result.output.lower()
    assert "--save-as" not in result.output
    assert "--alias" not in result.output


def test_hpc_help_includes_key_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["hpc", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "create" in result.output
    assert "status" in result.output
    assert "stop" in result.output


def test_resources_help_includes_key_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["resources", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "nodes" in result.output




def test_run_help_mentions_watch_and_priority_level() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["run", "--help"])
    assert result.exit_code == 0
    assert "Follow logs" in result.output
    assert "priority_level" in result.output


def test_job_logs_help_mentions_ssh_fast_path_and_workflow_fallback() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "logs", "--help"])
    assert result.exit_code == 0
    assert "SSH tunnel fast path" in result.output
    assert "Otherwise, fetches logs via GitHub workflow" in result.output


