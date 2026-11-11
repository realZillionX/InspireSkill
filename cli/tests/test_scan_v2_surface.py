"""Synthetic bundle evidence and offline compatibility tests."""

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "scan_v2_surface", Path(__file__).resolve().parents[2] / "scripts/scan_v2_surface.py"
)
assert _SPEC is not None and _SPEC.loader is not None
scanner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(scanner)

FIELDS = ["is_publicpath_readonly", "publicpath_mount_mode"]


def test_unchanged_action_field_migration():
    old = {"bundle": '"/api/v2/hpc?Action=CreateJobConsole"; x.is_publicpath_readonly=false;'}
    new = {
        "bundle": '"/api/v2/hpc?Action=CreateJobConsole"; delete x.is_publicpath_readonly; x.publicpath_mount_mode=2;'
    }
    assert scanner.extract_actions(old) == scanner.extract_actions(new)
    before = scanner.field_evidence(old, FIELDS)
    after = scanner.field_evidence(new, FIELDS)

    def row(rows, field):
        return next(r for r in rows if r["field"] == field)

    assert row(before, "publicpath_mount_mode")["token_presence"] == "no token evidence"
    assert row(after, "publicpath_mount_mode")["token_presence"] == "present"
    assert row(after, "is_publicpath_readonly")["token_presence"] == "present"


def test_no_source_values_or_substring_matches():
    rows = scanner.field_evidence(
        {"private-filename": '"private-cookie"; x.publicpath_mount_mode_extra=0;'}, FIELDS
    )
    report = json.dumps(rows)
    assert "private" not in report
    assert all(r["token_presence"] == "no token evidence" for r in rows)
    assert all(r["request_field_evidence"] == "not linked to a request body" for r in rows)


def test_local_mode_never_loads_session_or_fetches(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("offline scan attempted network/session access")

    monkeypatch.setattr(scanner.WebSession, "load", forbidden)
    for name in ("fetch_bundles", "discovery_actions", "probe"):
        monkeypatch.setattr(scanner, name, forbidden)
    (tmp_path / "synthetic.js").write_text(
        '"/api/v2/hpc?Action=CreateJobConsole"; publicpath_mount_mode:0;'
    )
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "scan",
            "--bundles",
            str(tmp_path),
            "--fields",
            ",".join(FIELDS),
            "--json",
            str(report),
        ],
    )
    assert scanner.main() == 0
    data = json.loads(report.read_text())
    assert {"discovery_version", "console", "rest_paths", "discovery", "probe"} <= data.keys()
    assert data["probe"] == {}
    assert data["field_evidence"] == scanner.field_evidence(
        {"synthetic": "publicpath_mount_mode:0;"}, FIELDS
    )
    assert "no token evidence" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["scan", "--bundles", str(tmp_path), "--probe"])
    with pytest.raises(SystemExit):
        scanner.main()


def test_send_read_swap_has_no_request_body_evidence():
    action = '"/api/v2/hpc?Action=CreateJobConsole";'
    old = {"bundle": action + "send({is_publicpath_readonly:false}); read.publicpath_mount_mode;"}
    new = {"bundle": action + "send({publicpath_mount_mode:0}); read.is_publicpath_readonly;"}
    assert scanner.extract_actions(old) == scanner.extract_actions(new)
    before = scanner.field_evidence(old, FIELDS)
    after = scanner.field_evidence(new, FIELDS)
    assert before == after
    assert all(row["token_presence"] == "present" for row in after)
    assert all(row["request_field_evidence"] == "not linked to a request body" for row in after)


def test_existing_mode_and_probe_remain_read_only(tmp_path, monkeypatch):
    session = object()
    monkeypatch.setattr(scanner.WebSession, "load", lambda **kwargs: session)
    monkeypatch.setattr(scanner, "_get_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(
        scanner,
        "fetch_bundles",
        lambda *args: {
            "synthetic": '"/api/v2/hpc?Action=CreateJobConsole"; '
            '"/api/v2/hpc?Action=GetJob"; " /api/v2/hpc?Action=GetAndDeleteJob";'
        },
    )
    monkeypatch.setattr(scanner, "discovery_actions", lambda *args: ("test", {"hpc": {"GetJob"}}))
    calls = []

    def request(*args, **kwargs):
        calls.append((args[2], kwargs["body"]))
        return {"Result": {}}

    monkeypatch.setattr(scanner, "_request_json", request)
    report = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["scan", "--probe", "--delay", "0", "--json", str(report)])
    assert scanner.main() == 0
    assert calls == [("/api/v2/hpc?Action=GetJob", {})]
    assert set(json.loads(report.read_text())) == {
        "discovery_version",
        "console",
        "rest_paths",
        "discovery",
        "probe",
    }


def test_requested_names_are_reported_once():
    assert scanner.field_evidence(
        {"synthetic": "x.custom_field = 7;"}, ["custom_field", "missing_field", "custom_field"]
    ) == [
        {
            "field": "custom_field",
            "token_presence": "present",
            "request_field_evidence": "not linked to a request body",
        },
        {
            "field": "missing_field",
            "token_presence": "no token evidence",
            "request_field_evidence": "not linked to a request body",
        },
    ]
