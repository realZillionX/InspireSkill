#!/usr/bin/env python3
"""Report the v2 Action surface the web console actually calls, and diff it.

Reads:  the console's JS bundles, `GET {base_url}/discovery`, and (with
        ``--probe``) the gateway itself, using the local `inspire` session.
Writes: nothing outside the cache directory passed to ``--cache``.

## Why this exists

`GET /discovery` is the obvious inventory and it is **not the whole surface**.
It lists 11 routes; the console calls 25. We once concluded from it that the
quota catalog — the request in front of every `create` — had no v2 counterpart,
wrote that into the reference, and kept the v1 dependency for months. The
Action was there the whole time under a route discovery does not mention.

So this is the third source, next to discovery and to guessing names: whatever
the console has hardcoded. It is a **lower bound** — dynamically assembled
calls are invisible to a regex — which makes it evidence that an Action exists
and never evidence that one does not.

It reports two shapes, because `/api/v2` has two. Most of it is
`?Action=`, but a REST-shaped surface sits beside it (`/api/v2/file/list`,
`/api/v2/train_job/remote_cmd`, four `.../instances/exec` PTY sockets), and an
inventory that only knows the Action shape calls those absent — which is
exactly the sentence we had written down about the remote shell.

## Use

    python3 scripts/scan_v2_surface.py                 # fetch, extract, diff
    python3 scripts/scan_v2_surface.py --probe         # + check each read-only
                                                       #   Action really routes
    python3 scripts/scan_v2_surface.py --json out.json # machine-readable

`--probe` sends one empty-body request per read-only Action (a few hundred) and
needs a logged-in session. Empty bodies are rejected at validation, so nothing
is created; write verbs are excluded twice over, by prefix allowlist and by a
verb blacklist. Read `references/dev/browser-api.md` §7 for how to read the
answers: only `InvalidAction: unknown action` means an Action is absent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import time
from collections import defaultdict

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "cli"))

from inspire.platform.web.browser_api.core import (  # noqa: E402
    _get_base_url,
    _request_json,
)
from inspire.platform.web.session import WebSession  # noqa: E402
from inspire.platform.web.session.requests import build_requests_session  # noqa: E402

#: `[A-Za-z0-9]+`, not `[A-Za-z]+`: the trailing digit of `GetProjectListV2` is
#: part of the name, and truncating it invents an Action that does not exist.
_HARDCODED = re.compile(r"/api/v2/([a-zA-Z0-9_-]+)\?Action=([A-Za-z0-9]+)")
#: `?Action=` is not the whole of `/api/v2`. A REST-shaped surface lives beside
#: it — `/api/v2/file/list`, `/api/v2/train_job/remote_cmd`, the four
#: `.../instances/exec` PTY sockets — and an inventory that only knows the
#: Action shape reports those as absent. That is how "no v2 counterpart for the
#: remote shell" got written down: true of Actions, false of `/api/v2`.
_REST_PATH = re.compile(r'"(/api/v2/[a-z0-9_][a-z0-9_/-]*)"')
_CHUNK_REF = re.compile(r'"(\./[^"]+\.js)"')
_ABS_CHUNK_REF = re.compile(r'"(/assets/[^"]+\.js)"')
_ENTRY = re.compile(r'(?:src|href)="([^"]+\.js)"')

_READ_PREFIXES = ("Get", "List", "Search")
_WRITE_VERBS = re.compile(
    r"Create|Delete|Update|Remove|Add|Stop|Start|Apply|Cancel|Submit|Operate|Scale"
    r"|Rollback|Save|Bind|Transfer|Publish|Import|Restart|Reset|Set|Modify|Move"
    r"|Rename|Upload|Kill|Terminate|Approve|Reject|Withdraw|Retry|Generate"
    r"|Disable|Enable|Commit|Mount|Preheat"
)


def is_read_only(action: str) -> bool:
    return action.startswith(_READ_PREFIXES) and not _WRITE_VERBS.search(action)


def fetch_bundles(session: WebSession, base_url: str, cache: pathlib.Path) -> dict[str, str]:
    """Walk the console's chunk graph from the entry script."""
    cache.mkdir(parents=True, exist_ok=True)
    http = build_requests_session(session, base_url)
    root = http.get(base_url + "/", timeout=60)
    root.raise_for_status()

    todo = list(dict.fromkeys(_ENTRY.findall(root.text)))
    seen: set[str] = set()
    texts: dict[str, str] = {}
    while todo:
        url = todo.pop(0)
        if url in seen:
            continue
        seen.add(url)
        cached = cache / f"{hashlib.md5(url.encode()).hexdigest()}.js"
        if cached.exists():
            text = cached.read_text(encoding="utf-8", errors="replace")
        else:
            try:
                response = http.get(base_url + url, timeout=90)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {url}: {type(exc).__name__}", file=sys.stderr)
                continue
            if response.status_code != 200:
                continue
            text = response.text
            cached.write_text(text, encoding="utf-8")
        texts[url] = text
        todo.extend("/assets/" + ref[2:] for ref in set(_CHUNK_REF.findall(text)))
        todo.extend(set(_ABS_CHUNK_REF.findall(text)))
    return texts


def extract_actions(texts: dict[str, str]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = defaultdict(set)
    for text in texts.values():
        for route, action in _HARDCODED.findall(text):
            found[route].add(action)
    return found


def extract_rest_paths(texts: dict[str, str]) -> set[str]:
    """`/api/v2` paths the console calls without an `?Action=` at all.

    A bare `/api/v2/<route>` is the prefix the Action calls are built from, so
    it is dropped; anything with a segment under it is a real endpoint.
    """
    paths: set[str] = set()
    for text in texts.values():
        for path in _REST_PATH.findall(text):
            if path.rstrip("/").count("/") > 3:
                paths.add(path)
    return paths


def discovery_actions(session: WebSession, base_url: str) -> tuple[str, dict[str, set[str]]]:
    payload = _request_json(
        session, "GET", "/discovery", referer=f"{base_url}/", timeout=30
    ).get("Result") or {}
    declared: dict[str, set[str]] = defaultdict(set)
    for service in payload.get("Services") or []:
        name = str(service.get("Name") or "")
        for action in service.get("Actions") or []:
            action_name = str(action.get("Name") or "") if isinstance(action, dict) else str(action)
            if name and action_name:
                declared[name].add(action_name)
    return str(payload.get("Version") or ""), declared


def probe(
    session: WebSession, base_url: str, actions: dict[str, set[str]], delay: float
) -> dict[str, dict[str, str]]:
    """Send one empty-body request per read-only Action and classify the answer."""
    verdicts: dict[str, dict[str, str]] = defaultdict(dict)
    for route in sorted(actions):
        for action in sorted(actions[route]):
            if not is_read_only(action):
                continue
            try:
                data = _request_json(
                    session,
                    "POST",
                    f"/api/v2/{route}?Action={action}",
                    referer=f"{base_url}/jobs/distributedTraining",
                    body={},
                    timeout=20,
                )
            except Exception as exc:  # noqa: BLE001
                verdicts[route][action] = f"EXC:{type(exc).__name__}"
            else:
                error = (data.get("ResponseMetadata") or {}).get("Error") or {}
                verdicts[route][action] = str(error.get("Code") or "OK")
            time.sleep(delay)
    return verdicts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="check each read-only Action routes")
    parser.add_argument("--delay", type=float, default=0.25, help="seconds between probes")
    parser.add_argument("--cache", default="", help="directory for downloaded chunks")
    parser.add_argument("--json", dest="json_out", default="", help="write the report here")
    args = parser.parse_args()

    session = WebSession.load(allow_expired=True)
    if session is None:
        print("No session. Run `inspire account check` first.", file=sys.stderr)
        return 2

    base_url = _get_base_url()
    cache = pathlib.Path(args.cache) if args.cache else _REPO_ROOT / ".scan-cache"
    texts = fetch_bundles(session, base_url, cache)
    console = extract_actions(texts)
    rest = extract_rest_paths(texts)
    version, declared = discovery_actions(session, base_url)

    total = sum(len(names) for names in console.values())
    print(f"chunks {len(texts)}  console: {len(console)} routes / {total} actions")
    print(f"discovery {version}: {len(declared)} services / "
          f"{sum(len(v) for v in declared.values())} actions")
    print(f"REST-shaped /api/v2 paths (no ?Action=): {len(rest)}")
    for path in sorted(rest):
        print(f"  {path}")
    print()

    verdicts = probe(session, base_url, console, args.delay) if args.probe else {}

    absent_routes = sorted(set(console) - set(declared))
    print(f"routes discovery does not declare ({len(absent_routes)}):")
    for route in absent_routes:
        print(f"  {route} ({len(console[route])})")

    if verdicts:
        tally: dict[str, int] = defaultdict(int)
        for by_action in verdicts.values():
            for verdict in by_action.values():
                tally[verdict] += 1
        print("\nprobe verdicts:")
        for verdict, count in sorted(tally.items(), key=lambda kv: -kv[1]):
            print(f"  {verdict:22s} {count}")
        missing = [
            f"{route}.{action}"
            for route, by_action in verdicts.items()
            for action, verdict in by_action.items()
            if verdict == "InvalidAction"
        ]
        print(f"  absent (InvalidAction): {missing or 0}")

    if args.json_out:
        pathlib.Path(args.json_out).write_text(
            json.dumps(
                {
                    "discovery_version": version,
                    "console": {r: sorted(a) for r, a in sorted(console.items())},
                    "rest_paths": sorted(rest),
                    "discovery": {s: sorted(a) for s, a in sorted(declared.items())},
                    "probe": verdicts,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
