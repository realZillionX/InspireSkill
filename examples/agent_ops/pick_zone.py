#!/usr/bin/env python3
"""Rank a single workspace's GPU job quotas from fresh CLI snapshots.

This optional example neither submits jobs nor predicts queue time. Supply the
capacity class of the effective priority explicitly; numeric priority mapping
and project caps are validated later by the core CLI. A ten-node request uses
--gpus 80 --nodes 10.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import shutil
import subprocess
import sys
from typing import Any


class QueryError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code if 0 < code < 256 else 1


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def nonnegative_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return number


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def envelope(stream: str) -> dict[str, Any] | None:
    try:
        value = json.loads(stream)
    except ValueError:
        return None
    return value if isinstance(value, dict) and isinstance(value.get("success"), bool) else None


def error_envelope(stream: str) -> dict[str, Any] | None:
    # CLI errors can follow diagnostic lines on stderr. Success stdout is strict.
    for line in stream.splitlines():
        if line.lstrip().startswith("{"):
            value = envelope(line)
            if value is not None and value["success"] is False:
                return value
    return None


def query(executable: str, args: list[str], account: str | None) -> list[dict[str, Any]]:
    command = [executable, "--json"]
    if account:
        command += ["--account", account]
    try:
        result = subprocess.run(
            command + args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
    except FileNotFoundError as exc:
        raise QueryError("inspire was not found on PATH", 127) from exc
    except subprocess.TimeoutExpired as exc:
        raise QueryError("inspire query timed out; no recommendation was made", 124) from exc
    data = envelope(result.stdout)
    failure = error_envelope(result.stderr)
    if data is not None and data["success"] is False:
        failure = data
    if failure is not None:
        error = failure.get("error") or {}
        message = error.get("message", "CLI query failed")
        raise QueryError(f"{error.get('type', 'CLIError')}: {message}", result.returncode or 1)
    if result.returncode:
        raise QueryError(f"inspire query exited {result.returncode}", result.returncode)
    if data is None or data.get("success") is not True:
        raise QueryError("invalid CLI JSON envelope; no recommendation was made")
    payload = data.get("data")
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise QueryError("CLI JSON is missing data.items; no recommendation was made")
    if payload.get("truncated") is True or payload.get("has_more") is True:
        raise QueryError("CLI returned an incomplete snapshot; no recommendation was made")
    items = payload["items"]
    if not all(isinstance(item, dict) for item in items):
        raise QueryError("CLI data.items contains malformed rows")
    return items


def quota_shape(row: dict[str, Any]) -> tuple[int, float, float]:
    try:
        pieces = str(row["quota"]).split(",")
        if len(pieces) != 3:
            raise ValueError
        gpu = int(pieces[0])
        cpu, memory = float(pieces[1]), float(pieces[2])
        if gpu < 0 or any(not math.isfinite(n) or n < 0 for n in (cpu, memory)):
            raise ValueError
        return gpu, cpu, memory
    except (KeyError, ValueError, TypeError) as exc:
        raise QueryError("malformed live quota row; no recommendation was made") from exc


def priority_eligibility(row: dict[str, Any], level: str) -> str:
    levels = row.get("allowed_priority_levels")
    if levels is None:
        return "unknown"
    if not isinstance(levels, list) or not all(isinstance(v, str) for v in levels):
        raise QueryError("malformed allowed_priority_levels in live quota row")
    if not levels:
        return "allowed"
    normalized = {v.lower() for v in levels}
    if level in normalized:
        return "allowed"
    if normalized.issubset({"low", "high"}):
        return "blocked"
    return "unknown"


def rank_groups(
    availability: list[dict[str, Any]], quotas: list[dict[str, Any]], *,
    gpus: int, nodes: int, priority_level: str, gpu_type: str | None = None,
    min_cpu: float = 0, min_memory_gib: float = 0,
) -> list[dict[str, Any]]:
    if gpus <= 0 or nodes <= 0 or gpus % nodes:
        raise QueryError("GPU and instance counts must be positive and evenly divisible", 2)
    per_instance = gpus // nodes
    if nodes > 1 and per_instance != 8:
        raise QueryError("multi-node jobs require an 8-GPU live quota per instance", 2)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for quota in quotas:
        gpu, cpu, memory = quota_shape(quota)
        if gpu != per_instance or cpu < min_cpu or memory < min_memory_gib:
            continue
        name = quota.get("compute_group")
        if not isinstance(name, str) or not name:
            raise QueryError("quota row has no compute_group")
        if gpu_type and gpu_type.casefold() not in str(quota.get("gpu_type", "")).casefold():
            continue
        grouped.setdefault(name, []).append(quota)

    candidates = []
    for group in availability:
        if group.get("kind") != "gpu":
            continue
        name = group.get("compute_group")
        if not isinstance(name, str) or not name:
            raise QueryError("availability row has no compute_group")
        if gpu_type and gpu_type.casefold() not in str(group.get("gpu_type", "")).casefold():
            continue
        matching = grouped.get(name, [])
        assessments = [(row, priority_eligibility(row, priority_level)) for row in matching]
        accepted = [row for row, eligibility in assessments if eligibility == "allowed"]
        unknown = [row for row, eligibility in assessments if eligibility == "unknown"]
        eligibility = "allowed" if accepted else ("unknown" if unknown else "blocked")
        whole_node = per_instance == 8
        warnings = []
        if whole_node:
            metric = "high_priority_free_nodes" if priority_level == "high" else "full_free_nodes"
            capacity = number(group.get(metric))
            if capacity is not None:
                capacity *= 8
            # These counters cover 8-GPU nodes, including within mixed-size groups.
        else:
            metric = (
                "high_priority_available_gpus" if priority_level == "high" else "available_gpus"
            )
            capacity = number(group.get(metric))
            warnings.append("GPU balance is not physical placement or fragmentation evidence")
        if not matching:
            warnings.append("no matching live quota row for the requested instance shape")
        elif eligibility == "unknown":
            warnings.append("priority or node eligibility is unknown; validate the create plan")
        if capacity is None:
            warnings.append("required capacity field is unavailable")
        fits = capacity is not None and capacity >= gpus
        candidates.append({
            "group": name, "gpu_type": group.get("gpu_type"),
            "eligible": eligibility, "capacity_metric": metric,
            "capacity_gpus": capacity, "snapshot_fits": fits,
            "free_nodes": group.get("free_nodes"),
            "full_free_nodes": group.get("full_free_nodes"),
            "reclaimable_nodes": group.get("reclaimable_nodes"),
            "high_priority_free_nodes": group.get("high_priority_free_nodes"),
            "matching_quotas": [row["quota"] for row in (accepted or unknown)],
            "warnings": warnings,
        })
    candidates.sort(key=lambda row: (
        row["eligible"] == "allowed" and row["snapshot_fits"],
        {"allowed": 2, "unknown": 1, "blocked": 0}[row["eligible"]],
        row["capacity_gpus"] if row["capacity_gpus"] is not None else -float("inf"),
        row["free_nodes"] or 0,
    ), reverse=True)
    return candidates


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--account", help="use the same account for both live queries")
    ap.add_argument("--gpus", type=positive_int, required=True, help="total GPUs requested")
    ap.add_argument("--nodes", type=positive_int, default=1, help="job instances (default: 1)")
    ap.add_argument("--priority-level", choices=("low", "high"), required=True,
                    help="capacity class of the effective priority; numeric mapping is not inferred")
    ap.add_argument("--gpu-type", help="case-insensitive GPU model substring")
    ap.add_argument("--min-cpu", type=nonnegative_number, default=0, help="CPU cores per instance")
    ap.add_argument("--min-memory-gib", type=nonnegative_number, default=0,
                    help="host memory per instance in GiB (not GPU memory)")
    ap.add_argument("--top", type=positive_int, default=3)
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    args = ap.parse_args(argv)
    if args.gpus % args.nodes:
        ap.error("--gpus must be divisible by --nodes")
    if args.nodes > 1 and args.gpus // args.nodes != 8:
        ap.error("multi-node jobs require 8 GPUs per instance; verify the live quota")
    try:
        executable = shutil.which("inspire")
        if not executable:
            raise QueryError("inspire was not found on PATH", 127)
        availability = query(executable, [
            "resources", "availability", "--workspace", args.workspace, "--all",
        ], args.account)
        quotas = query(executable, [
            "job", "quota", "--workspace", args.workspace, "--all",
        ], args.account)
        candidates = rank_groups(
            availability, quotas, gpus=args.gpus, nodes=args.nodes,
            priority_level=args.priority_level, gpu_type=args.gpu_type,
            min_cpu=args.min_cpu, min_memory_gib=args.min_memory_gib,
        )
    except QueryError as exc:
        if args.json:
            print(json.dumps({"success": False, "error": {
                "type": "QueryError", "message": str(exc), "code": exc.code,
            }}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return exc.code
    recommended = next((row for row in candidates
                        if row["eligible"] == "allowed" and row["snapshot_fits"]), None)
    result = {
        "success": True,
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "request": vars(args), "recommended": recommended,
        "candidates": candidates[:args.top], "candidate_count": len(candidates),
        "limitations": [
            "capacity snapshots are not reservations or queue-time predictions",
            "project/member/parent quota headroom is not inferred from resources usage",
            "validate numeric priority, image, CPU/memory, network and placement via dry-run/events",
        ],
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for row in result["candidates"]:
            print(f"{row['group']}: {row['capacity_metric']} -> "
                  f"{row['capacity_gpus']} GPU, priority={row['eligible']}, "
                  f"snapshot_fits={row['snapshot_fits']}")
        print(f"Recommended for validation: {recommended['group']}" if recommended else
              "No confirmed fit in this snapshot; inspect events or re-check before changing needs.")
        for limitation in result["limitations"]:
            print(f"Note: {limitation}")
    return 0 if recommended else 1


if __name__ == "__main__":
    raise SystemExit(main())
