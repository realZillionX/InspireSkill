"""Console name contracts apply before live lookup, dry-run and batch submission."""

import json

import pytest
from click.testing import CliRunner

from inspire.cli.main import main
from inspire.services.utils.workload_names import WorkloadNameError, validate_workload_name


LIMITS = {"notebook": 32, "ray": 32, "serving": 32, "job": 255, "hpc": 255, "tensorboard": 255}


@pytest.mark.parametrize("kind,maximum", LIMITS.items())
def test_name_length_boundaries(kind, maximum):
    validate_workload_name("a" * maximum, kind)
    with pytest.raises(WorkloadNameError, match=str(maximum)):
        validate_workload_name("a" * (maximum + 1), kind)


@pytest.mark.parametrize("kind", LIMITS)
@pytest.mark.parametrize("name", ["", "has space", "a/b", "a\n", "emoji😀"])
def test_invalid_characters(kind, name):
    with pytest.raises(WorkloadNameError, match=str(LIMITS[kind])):
        validate_workload_name(name, kind)


@pytest.mark.parametrize("kind", ["notebook", "ray", "serving"])
@pytest.mark.parametrize("name", ["Upper", "1abc", "a_b"])
def test_lowercase_workloads_reject_invalid_patterns(kind, name):
    with pytest.raises(WorkloadNameError):
        validate_workload_name(name, kind)


def test_workload_patterns_are_distinct():
    validate_workload_name("a.b-1", "serving")
    for kind in ("notebook", "ray"):
        with pytest.raises(WorkloadNameError):
            validate_workload_name("a.b", kind)
    for kind in ("job", "hpc", "tensorboard"):
        validate_workload_name("中文_A-1.2", kind)
        validate_workload_name("a" * 33, kind)


@pytest.mark.parametrize(
    "kind,dry_run",
    [
        (kind, dry)
        for kind in LIMITS
        for dry in (False, True)
        if not dry or kind not in ("notebook", "tensorboard")
    ],
)
@pytest.mark.parametrize("bad_name", ["overlong", "invalid/characters"])
def test_create_rejects_bad_names_locally(kind, dry_run, bad_name):
    args = [
        "--json",
        kind,
        "create",
        "--name",
        ("a" * (LIMITS[kind] + 1) if bad_name == "overlong" else bad_name),
        "--workspace",
        "cpu",
        "--project",
        "project",
        "--group",
        "group",
    ]
    if kind == "tensorboard":
        args += ["--summary-path", "/inspire/runs"]
    else:
        args += ["--quota", "0,1,2", "--image", "image"]
    if kind == "hpc":
        args += ["--entrypoint", "srun true"]
    if kind in ("job", "ray", "serving"):
        args += ["--command", "true"]
    if kind == "serving":
        args += ["--model", "model", "--port", "8000"]
    if dry_run:
        args += ["--dry-run"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 12, result.output
    error = json.loads(result.output)["error"]
    assert error["type"] == "ValidationError"
    assert str(LIMITS[kind]) in error["message"]


@pytest.mark.parametrize("kind", ["job", "hpc", "notebook", "ray", "serving"])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("bad_name", ["overlong", "invalid/characters"])
def test_batch_validates_all_names_before_any_live_work(tmp_path, kind, dry_run, bad_name):
    path = tmp_path / "batch.json"
    key = {
        "job": "jobs",
        "hpc": "jobs",
        "notebook": "notebooks",
        "ray": "jobs",
        "serving": "servings",
    }[kind]
    invalid = "a" * (LIMITS[kind] + 1) if bad_name == "overlong" else bad_name
    path.write_text(json.dumps({key: [{"name": "valid"}, {"name": invalid}]}))
    args = ["--json", kind, "batch", str(path)] + (["--dry-run"] if dry_run else [])
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 12, result.output
    error = json.loads(result.output)["error"]
    assert error["type"] == "ValidationError"
    assert str(LIMITS[kind]) in error["message"]


def test_ray_worker_name_uses_the_same_console_contract():
    from inspire.services.ray.ray_submission import parse_worker_spec

    for name in ("a" * 33, "Upper", "a.b"):
        with pytest.raises(WorkloadNameError, match="32"):
            parse_worker_spec(
                f"name={name};image=image;group=group;quota=0,1,2;min=1;max=1"
            )
