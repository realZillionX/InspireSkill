#!/usr/bin/env python3
"""Compare reviewed request-field snapshots, or check a local JSON body against one."""

import argparse
from datetime import date
import json
from pathlib import Path
import re


JSON_TYPES = {
    "array": (list,),
    "boolean": (bool,),
    "integer": (int,),
    "null": (type(None),),
    "number": (float, int),
    "object": (dict,),
    "string": (str,),
    "absent": (),
}


def load_snapshot(path: Path) -> dict:
    """Read reviewed schema metadata. Examples and defaults are outside its scope."""
    snapshot = json.loads(path.read_bytes())
    if (
        not isinstance(snapshot, dict)
        or not isinstance(snapshot.get("source"), str)
        or not snapshot["source"].strip()
    ):
        raise ValueError
    date.fromisoformat(snapshot["observed_on"])
    actions, fields = snapshot["actions"], snapshot["fields"]
    if not isinstance(actions, list) or not actions or not isinstance(fields, dict):
        raise ValueError
    if not all(re.fullmatch(r"[a-zA-Z0-9_-]+\.[A-Za-z][A-Za-z0-9]*", a) for a in actions):
        raise ValueError
    for name, rule in fields.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or not isinstance(rule, dict):
            raise ValueError
        types = JSON_TYPES[rule["type"]]
        if rule.keys() - {"type", "enum"}:
            raise ValueError
        if "enum" in rule:
            values = rule["enum"]
            if not isinstance(values, list) or not values:
                raise ValueError
            if not all(type(v) in types and not isinstance(v, (dict, list)) for v in values):
                raise ValueError
    return snapshot


def compare(before: dict, after: dict) -> dict:
    old_actions, new_actions = set(before["actions"]), set(after["actions"])
    common = old_actions & new_actions
    changes = {}
    added, removed = [], []
    old, new = before["fields"], after["fields"]
    comparable_fields = sorted(old.keys() | new.keys()) if common else []
    for field in comparable_fields:
        if field not in old or field not in new:
            changes[field] = {"no_field_evidence": "before" if field not in old else "after"}
            continue
        left, right = old[field], new[field]
        delta = {}
        if left["type"] != right["type"]:
            delta["type"] = [left["type"], right["type"]]
            if left["type"] == "absent":
                added.append(field)
            elif right["type"] == "absent":
                removed.append(field)
        if "enum" in left and "enum" in right:
            # Compare enum membership without including any sample or enum literals in reports.
            # JSON booleans and numbers are distinct even though False == 0 in Python.
            lhs = {(type(v) is bool, v) for v in left["enum"]}
            rhs = {(type(v) is bool, v) for v in right["enum"]}
            if lhs != rhs:
                delta["enum"] = {"added": len(rhs - lhs), "removed": len(lhs - rhs)}
        elif ("enum" in left) != ("enum" in right):
            delta["enum_evidence"] = "before only" if "enum" in left else "after only"
        if delta:
            changes[field] = delta
    return {
        "observed_on": [before["observed_on"], after["observed_on"]],
        "compared_actions": sorted(common),
        "actions_without_comparable_evidence": sorted(old_actions ^ new_actions),
        "fields": changes,
        "added": added,
        "removed": removed,
        "rename_candidates": [[removed[0], added[0]]] if len(added) == len(removed) == 1 else [],
    }


def check_request(snapshot: dict, action: str, body: dict) -> dict[str, str]:
    if not isinstance(body, dict) or action not in snapshot["actions"]:
        raise ValueError
    results = {}
    for name, rule in snapshot["fields"].items():
        if name not in body:
            results[name] = "omitted"
        elif rule["type"] == "absent":
            results[name] = "unexpected field"
        elif type(body[name]) not in JSON_TYPES[rule["type"]]:
            results[name] = "wrong type"
        elif "enum" in rule and body[name] not in rule["enum"]:
            results[name] = "outside enum"
        else:
            results[name] = "matches"
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path, help="reviewed field snapshot")
    parser.add_argument("input", type=Path, help="second snapshot or local request body")
    parser.add_argument("--request", metavar="SERVICE.ACTION", help="check one request body")
    args = parser.parse_args()
    try:
        baseline = load_snapshot(args.baseline)
        if args.request:
            result = check_request(baseline, args.request, json.loads(args.input.read_bytes()))
            status = int(any(v not in {"omitted", "matches"} for v in result.values()))
        else:
            result = compare(baseline, load_snapshot(args.input))
            status = 0
    except (KeyError, OSError, TypeError, ValueError):
        parser.error("expected a reviewed field snapshot or a JSON object for a covered Action")
    print(json.dumps(result, indent=2, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
