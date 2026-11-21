"""Exercise the maintainer's offline report with synthetic platform evidence."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/compare_request_contracts.py"
CONTRACT = ROOT / "references/dev/contracts/public-path-mount.json"


def test_field_migration_and_partial_evidence(tmp_path):
    current = json.loads(CONTRACT.read_bytes())
    previous = {
        **current,
        "actions": ["hpc.CreateJobConsole", "hpc.ListJobs"],
        "fields": {
            "is_publicpath_readonly": {"type": "boolean"},
            "publicpath_mount_mode": {"type": "absent"},
            "priority": {"type": "integer", "enum": [0, 1]},
            "counter": {"type": "string"},
        },
    }
    current["fields"].update(
        priority={"type": "integer", "enum": [1, 2]}, counter={"type": "integer"}
    )
    previous["fields"]["unreviewed"] = {"type": "string"}
    files = [tmp_path / "before.json", tmp_path / "after.json"]
    for path, snapshot in zip(files, [previous, current]):
        path.write_text(json.dumps(snapshot), encoding="utf-8")
    report = json.loads(subprocess.check_output([sys.executable, str(SCRIPT), *files]))
    assert report["compared_actions"] == ["hpc.CreateJobConsole"]
    assert "hpc.ListJobs" in report["actions_without_comparable_evidence"]
    assert report["removed"] == ["is_publicpath_readonly"]
    assert report["added"] == ["publicpath_mount_mode"]
    assert report["rename_candidates"] == [["is_publicpath_readonly", "publicpath_mount_mode"]]
    assert report["fields"]["counter"] == {"type": ["string", "integer"]}
    assert report["fields"]["priority"] == {"enum": {"added": 1, "removed": 1}}
    assert report["fields"]["unreviewed"] == {"no_field_evidence": "after"}


@pytest.mark.parametrize(
    "body,expected,code",
    [
        ({}, {"publicpath_mount_mode": "omitted"}, 0),
        ({"publicpath_mount_mode": 0}, {"publicpath_mount_mode": "matches"}, 0),
        ({"publicpath_mount_mode": True}, {"publicpath_mount_mode": "wrong type"}, 1),
        ({"publicpath_mount_mode": 3}, {"publicpath_mount_mode": "outside enum"}, 1),
        ({"is_publicpath_readonly": False}, {"is_publicpath_readonly": "unexpected field"}, 1),
    ],
)
def test_sample_report_projects_only_reviewed_fields(tmp_path, body, expected, code):
    body.update(cookie="private-cookie", job_id="private-job", unrelated={"private-key": 3})
    sample = tmp_path / "private-file.json"
    sample.write_text(json.dumps(body), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(CONTRACT),
            str(sample),
            "--request",
            "hpc.CreateJobConsole",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == code
    assert "private" not in result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert set(report) == {"is_publicpath_readonly", "publicpath_mount_mode"}
    assert expected.items() <= report.items()
