"""JSON reporting helpers for `inspire init` command."""

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


def resolve_write_state(
    before: dict[str, dict[str, int | bool]],
    after_path: Path,
) -> tuple[bool, bool]:
    """Return (written, skipped_existing) for a target config path."""
    key = str(after_path)
    prev = before.get(key, {"exists": False, "mtime_ns": 0})
    prev_exists = bool(prev.get("exists"))
    prev_mtime_ns = int(prev.get("mtime_ns", 0))
    now_exists = after_path.exists()
    if not now_exists:
        return False, bool(prev_exists)
    now_mtime_ns = after_path.stat().st_mtime_ns
    written = (not prev_exists) or (now_mtime_ns > prev_mtime_ns)
    skipped = bool(prev_exists and not written)
    return written, skipped


def build_next_steps(mode: str) -> list[str]:
    """Build mode-specific suggested next steps for JSON payloads."""
    if mode == "discover":
        return [
            'Ensure a password is available via INSPIRE_PASSWORD or [accounts."<username>"].password',
            "Run: inspire config show",
        ]
    return [
        "Set INSPIRE_USERNAME and INSPIRE_PASSWORD if needed",
        "Run: inspire config show",
    ]


def emit_init_json(
    *,
    mode: str,
    target_paths: list[Path],
    before: dict[str, dict[str, int | bool]],
    detected: list[tuple],
    warnings: list[str],
    effective_json: bool,
    discover: dict[str, object] | None = None,
) -> None:
    """Emit machine-readable init summary when JSON output is enabled."""
    if not effective_json:
        return

    files_written: list[str] = []
    files_skipped: list[str] = []
    for path in target_paths:
        written, skipped = resolve_write_state(before, path)
        if written:
            files_written.append(str(path))
        elif skipped:
            files_skipped.append(str(path))

    secret_count = 0
    for option, _ in detected:
        if getattr(option, "secret", False):
            secret_count += 1

    payload: dict[str, object] = {
        "mode": mode,
        "files_written": files_written,
        "files_skipped": files_skipped,
        "detected_env_count": len(detected),
        "secret_env_count": secret_count,
        "warnings": warnings,
        "next_steps": build_next_steps(mode),
    }
    if discover is not None:
        payload["discover"] = discover

    click.echo(json_formatter.format_json(payload, success=True))
