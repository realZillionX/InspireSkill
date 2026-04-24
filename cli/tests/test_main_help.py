from click.testing import CliRunner

from inspire.cli.formatters.human_formatter import format_job_status
from inspire.cli.main import main as cli_main


def test_root_help_explains_global_json_position() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--help"])

    assert result.exit_code == 0
    assert "before the subcommand" in result.output
    assert "inspire --json hpc status <name>" in result.output


def test_job_status_formatter_shows_platform_priority_fields() -> None:
    output = format_job_status(
        {
            "job_id": "job-123",
            "name": "demo",
            "status": "RUNNING",
            "running_time_ms": "1000",
            "priority": 10,
            "priority_name": "10",
            "priority_level": "HIGH",
        }
    )

    assert "Requested Priority: 10" in output
    assert "Priority Name: 10" in output
    assert "Priority Level: HIGH" in output
