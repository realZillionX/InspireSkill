"""Compact result reporting helpers for `inspire init`."""

from __future__ import annotations

from pathlib import Path

import click

from inspire.cli.formatters import json_formatter


def snapshot_paths(global_path: Path, project_path: Path) -> dict[str, dict[str, int | bool]]:
    """Capture path existence and mtime before init mutates config files."""
    snapshot: dict[str, dict[str, int | bool]] = {}
    for path in (global_path, project_path):
        exists = path.exists()
        snapshot[str(path)] = {
            "exists": exists,
            "mtime_ns": path.stat().st_mtime_ns if exists else 0,
        }
    return snapshot


def _path_was_written(
    before: dict[str, dict[str, int | bool]],
    after_path: Path,
) -> bool:
    """Return whether init created or changed a target config path."""
    key = str(after_path)
    prev = before.get(key, {"exists": False, "mtime_ns": 0})
    prev_exists = bool(prev.get("exists"))
    prev_mtime_ns = int(prev.get("mtime_ns", 0))
    now_exists = after_path.exists()
    if not now_exists:
        return False
    now_mtime_ns = after_path.stat().st_mtime_ns
    return (not prev_exists) or (now_mtime_ns > prev_mtime_ns)


def emit_init_result(
    *,
    target_paths: list[Path],
    before: dict[str, dict[str, int | bool]],
    warnings: list[str],
    effective_json: bool,
) -> None:
    """Emit one compact init result without exposing local file-system paths."""
    files_written = sum(_path_was_written(before, path) for path in target_paths)

    payload: dict[str, object] = {
        "status": "updated" if files_written else "unchanged",
    }
    if warnings:
        payload["warnings"] = warnings

    if effective_json:
        click.echo(json_formatter.format_json(payload))
        return
    click.echo(f"Configuration {payload['status']}.")
