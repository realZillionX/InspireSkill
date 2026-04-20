"""Diff a captured JSONL against the KNOWN endpoint set.

Prints three sections:
  1. NEW real endpoints (status 200 or 4xx business error; not in KNOWN)
  2. KNOWN but 404 this run (platform may have retired them; suspect stale)
  3. KNOWN but never triggered (destructive or lazy — cross-ref with CLI)

Usage:
    uv run python scripts/reverse_capture/analyze.py /tmp/bapi.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from known_endpoints import KNOWN, STALE_SINCE_2026_04, normalize_path


def _is_real(records: list[dict]) -> bool:
    """Endpoint exists if any record returned a non-404 200, or a 4xx with a
    business error code instead of the nginx '404 page not found' body."""
    for r in records:
        status = r.get("status")
        body = (r.get("response_body") or "")
        if status == 200 and "404 page not found" not in body.lower():
            return True
        if status is not None and 400 <= status < 500 and "404 page not found" not in body.lower():
            return True
    return False


def _best_sample(records: list[dict]) -> dict:
    for r in records:
        if r.get("status") == 200 and "404 page not found" not in (r.get("response_body") or "").lower():
            return r
    return records[0]


def _summary(sample: dict) -> str:
    body = sample.get("response_body") or ""
    try:
        parsed = json.loads(body)
    except Exception:
        return f"body[:100]={body[:100]!r}"
    code = parsed.get("code")
    data = parsed.get("data")
    if isinstance(data, list):
        return f"code={code} data=list[{len(data)}]"
    if isinstance(data, dict):
        return f"code={code} data_keys={list(data.keys())[:8]}"
    return f"code={code} msg={(parsed.get('message') or '')[:50]!r}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", nargs="+", help="One or more capture JSONL paths (merged)")
    args = ap.parse_args()

    records: list[dict] = []
    for path in args.jsonl:
        p = Path(path)
        if not p.exists():
            print(f"skip (missing): {p}")
            continue
        for line in p.read_text().splitlines():
            if line.strip():
                records.append(json.loads(line))
    if not records:
        print("no records")
        return 1

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in records:
        groups[(r["method"].upper(), normalize_path(r["path"]))].append(r)

    real = {k: v for k, v in groups.items() if _is_real(v)}
    fake_404s = {k: v for k, v in groups.items() if not _is_real(v)}

    new_real = {k: v for k, v in real.items() if k not in KNOWN}
    known_seen = {k: v for k, v in real.items() if k in KNOWN}
    known_unseen = KNOWN - set(real.keys())

    stale_resurrected = {k for k in real if k in STALE_SINCE_2026_04}
    known_404_now = {k for k in fake_404s if k in KNOWN}

    print("=" * 72)
    print(f"{len(records)} records · {len(groups)} unique (method, path-template) pairs")
    print(f"  REAL: {len(real)}  (known: {len(known_seen)}, NEW: {len(new_real)})")
    print(f"  KNOWN not triggered: {len(known_unseen)}")
    print(f"  KNOWN now 404 (stale suspect): {len(known_404_now)}")
    print(f"  Stale endpoint resurrected: {len(stale_resurrected)}")
    print("=" * 72)

    if new_real:
        print(f"\n=== NEW endpoints ({len(new_real)}) ===")
        for (m, t), v in sorted(new_real.items()):
            sample = _best_sample(v)
            status = sample.get("status")
            body_hint = ""
            if sample.get("request_body") and (sample["request_body"] or "").startswith("{"):
                try:
                    keys = list(json.loads(sample["request_body"]).keys())[:6]
                    body_hint = f"  body_keys={keys}"
                except Exception:
                    pass
            print(f"  {m:6s} {t}  [{status}]  {_summary(sample)}{body_hint}")

    if known_404_now:
        print(f"\n=== KNOWN now 404 (likely stale on the platform) ({len(known_404_now)}) ===")
        for (m, t) in sorted(known_404_now):
            print(f"  {m:6s} {t}")

    if stale_resurrected:
        print(f"\n=== STALE endpoints resurrected ({len(stale_resurrected)}) ===")
        for (m, t) in sorted(stale_resurrected):
            print(f"  {m:6s} {t}  (platform re-enabled? update known_endpoints.py)")

    if known_unseen:
        print(f"\n=== KNOWN not triggered ({len(known_unseen)}) ===")
        print("  (destructive endpoints like create/delete or lazy endpoints like image/update)")
        for (m, t) in sorted(known_unseen):
            note = ""
            if (m, t) in STALE_SINCE_2026_04:
                note = "  # stale since 2026-04"
            print(f"  {m:6s} {t}{note}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
