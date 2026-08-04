from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from inspire.cli.commands import workload_profile
from inspire.cli.main import main as cli_main


def _patch_profile_store(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        workload_profile,
        "load_project_profile_data",
        lambda: (tmp_path / ".inspire" / "config.toml", {}),
    )


def _set_args() -> list[str]:
    return [
        "job",
        "profile",
        "set",
        "train",
        "--workspace",
        "GPU Workspace",
        "--project",
        "Research",
        "--group",
        "H200 Group",
        "--quota",
        "1,20,200",
        "--image",
        "train:v1",
    ]


def test_profile_set_human_output_hides_config_path(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    _patch_profile_store(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli_main, _set_args())

    assert result.exit_code == 0, result.output
    assert result.output == "Saved job profile: train\n"
    assert str(tmp_path) not in result.output


def test_profile_set_json_output_hides_config_path(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    _patch_profile_store(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli_main, ["--json", *_set_args()])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"] == {
        "name": "train",
        "profile": {
            "workspace": "GPU Workspace",
            "project": "Research",
            "group": "H200 Group",
            "quota": "1,20,200",
            "image": "train:v1",
        },
    }
    assert str(tmp_path) not in result.output


@pytest.mark.parametrize(
    ("option", "value", "resource_name"),
    (
        ("--workspace", "ws-123456", "workspace"),
        ("--project", "project-123456", "project"),
        ("--group", "lcg-123456", "compute group"),
        ("--quota", "quota-123456", "quota"),
        ("--image", "image-123456", "image"),
    ),
)
def test_profile_set_rejects_id_shaped_references(
    monkeypatch,
    tmp_path,
    option: str,
    value: str,
    resource_name: str,
) -> None:  # noqa: ANN001
    _patch_profile_store(monkeypatch, tmp_path)

    args = _set_args()
    args[args.index(option) + 1] = value
    result = CliRunner().invoke(cli_main, args)

    assert result.exit_code == 12, result.output
    assert f"only accept a {resource_name} name" in result.output
    assert "handle" not in result.output.lower()
    assert value not in result.output
    assert not (tmp_path / ".inspire" / "config.toml").exists()
