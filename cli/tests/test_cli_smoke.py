
from click.testing import CliRunner

from inspire.cli.main import main as cli_main


def test_cli_help_includes_top_level_groups() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--help"])
    assert result.exit_code == 0
    for group in ("job", "notebook", "image", "resources", "hpc"):
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
    removed_default_cmd = "set" + "-default"
    assert removed_default_cmd not in result.output


def test_notebook_ssh_help_is_black_box_no_save_as() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "ssh", "--help"])
    assert result.exit_code == 0
    assert "establishes and\n  caches the connection" in result.output
    assert "bootstrap" not in result.output.lower()
    assert "rtunnel" not in result.output.lower()
    assert "sshd" not in result.output.lower()
    assert "--save-as" not in result.output
    assert "--alias" not in result.output


def test_hpc_help_includes_key_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["hpc", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "create" in result.output
    assert "instances" in result.output
    assert "status" in result.output
    assert "stop" in result.output


def test_resources_help_includes_key_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["resources", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "nodes" in result.output


def test_job_logs_help_mentions_ssh_only() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "logs", "--help"])
    assert result.exit_code == 0
    assert "over SSH" in result.output
    assert "cached notebook connection" in result.output
