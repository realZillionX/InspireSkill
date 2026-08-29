"""Compact result reporting helpers for `inspire init`."""

from __future__ import annotations

import hashlib
from pathlib import Path

import click

from inspire.cli.formatters import json_formatter


def _content_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_paths(*paths: Path) -> dict[str, dict[str, int | bool | str]]:
    """Capture path existence and content before init mutates config files."""
    snapshot: dict[str, dict[str, int | bool | str]] = {}
    for path in paths:
        exists = path.exists()
        snapshot[str(path)] = {
            "exists": exists,
            "mtime_ns": path.stat().st_mtime_ns if exists else 0,
            "digest": _content_digest(path) if exists else "",
        }
    return snapshot


def _path_was_written(
    before: dict[str, dict[str, int | bool | str]],
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
    if not prev_exists:
        return True
    prev_digest = str(prev.get("digest") or "")
    if prev_digest:
        return _content_digest(after_path) != prev_digest
    now_mtime_ns = after_path.stat().st_mtime_ns
    return now_mtime_ns > prev_mtime_ns


def emit_init_result(
    *,
    target_paths: list[Path],
    before: dict[str, dict[str, int | bool | str]],
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
