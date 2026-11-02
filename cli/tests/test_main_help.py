
from inspire.cli.formatters.human_formatter import format_job_status


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
    assert "job-123" not in output
    assert "Job ID" not in output
