from __future__ import annotations

import pytest
from click.testing import CliRunner

from inspire.cli.commands.job import job_events
from inspire.cli.main import main as cli_main


@pytest.mark.parametrize(
    "instance_name",
    [
        "550e8400-e29b-41d4-a716-446655440000",
        "pod-1234abcd",
    ],
)
def test_job_events_rejects_instance_handles_before_api(
    monkeypatch, instance_name: str
) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        job_events.Config,
        "from_files_and_env",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("instance validation should run before config")
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            "events",
            "train-a",
            "--workspace",
            "Test Workspace",
            "--instance",
            instance_name,
        ],
    )

    assert result.exit_code != 0
    assert "job instance name" in result.output
    assert instance_name not in result.output


def test_pure_hex_instance_name_remains_a_name() -> None:
    assert job_events._reject_job_instance_name(
        job_events.Context(),
        "1234abcd",
    ) == "1234abcd"
