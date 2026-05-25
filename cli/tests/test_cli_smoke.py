
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
        "list",
        "status",
        "ssh",
        "exec",
        "scp",
        "shell",
        "install-deps",
        "metrics",
        "events",
        "lifecycle",
    ):
        assert sub in result.output, f"missing: {sub}\n{result.output}"
    for removed in ("connections", "refresh", "forget", "test", "top"):
        assert f"\n  {removed} " not in result.output
    removed_default_cmd = "set" + "-default"
    assert removed_default_cmd not in result.output


def test_notebook_ssh_help_is_human_ssh_entrypoint_with_compat_commands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "ssh", "--help"])
    assert result.exit_code == 0
    assert "Open SSH to a notebook or run a remote command" in result.output
    for subcommand in ("connect", "refresh", "forget", "test"):
        assert f"\n  {subcommand} " in result.output
    for removed in ("list", "status", "exec", "shell", "scp", "install-deps"):
        assert f"\n  {removed} " not in result.output
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
    assert "availability" in result.output
    assert "nodes" in result.output


def test_job_logs_help_mentions_platform_default() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "logs", "--help"])
    assert result.exit_code == 0
    assert "--source [platform|ssh]" in result.output
    assert "Platform logs are the default" in result.output
