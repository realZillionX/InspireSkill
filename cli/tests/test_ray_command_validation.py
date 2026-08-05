from __future__ import annotations

import pytest
from click import BadParameter
from click.testing import CliRunner

from inspire.cli.commands.ray.ray_commands import _parse_worker_spec
from inspire.cli.main import main as cli_main


def test_ray_worker_spec_accepts_strict_schema() -> None:
    spec = _parse_worker_spec(
        "name=w1;image=ray:v1;group=Full Group;quota=0,4,16;min=1;max=3;"
        "image-type=SOURCE_PRIVATE;shm-size=32"
    )

    assert spec["image_type"] == "SOURCE_PRIVATE"
    assert spec["shm_size"] == 32
    assert spec["min"] == 1
    assert spec["max"] == 3


@pytest.mark.parametrize(
    "raw, message",
    [
        (
            "name=w1;image=ray:v1;group=Full Group;quota=0,4,16;min=0;max=3",
            "worker min and max must be >= 1",
        ),
        (
            "name=w1;image=ray:v1;group=Full Group;quota=0,4,16;min=2;max=1",
            "worker max must be >= min",
        ),
        (
            "name=w1;image=ray:v1;group=Full Group;quota=0,4,16;min=1;max=3;shm=32",
            "Use worker keys image-type and shm-size",
        ),
        (
            "name=w1;image=ray:v1;group=Full Group;quota=0,4,16;min=1;max=3;image-type=BAD",
            "worker image-type must be one of",
        ),
    ],
)
def test_ray_worker_spec_rejects_loose_or_invalid_schema(raw: str, message: str) -> None:
    with pytest.raises(BadParameter, match=message):
        _parse_worker_spec(raw)


def test_ray_create_help_uses_named_condition_flags() -> None:
    result = CliRunner().invoke(cli_main, ["ray", "create", "--help"])

    assert result.exit_code == 0
    assert "--image NAME|URL" in result.output
    assert "--group NAME" in result.output
    assert "--quota SPEC" in result.output
    assert "shm-size" in result.output


@pytest.mark.parametrize("command", ["status", "events", "instances", "stop", "delete"])
def test_ray_name_commands_reject_handles_before_web_session(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    from inspire.cli.commands.ray import ray_commands as ray_mod

    def fail_session():  # noqa: ANN001
        raise AssertionError("web session should not be opened for handle-shaped input")

    monkeypatch.setattr(ray_mod, "get_web_session", fail_session)

    args = [
        "--json",
        "ray",
        command,
        "ray-c4eb3ac3-6d83-405c-aa29-059bc945c4bf",
        "--workspace",
        "cpu-room",
    ]
    if command == "delete":
        args.append("--yes")

    result = CliRunner().invoke(cli_main, args)

    assert result.exit_code != 0
    assert "ValidationError" in result.output
    assert "ray name" in result.output
