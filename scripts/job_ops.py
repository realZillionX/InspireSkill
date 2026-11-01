#!/usr/bin/env python3
"""job_ops: wrap `inspire job create/stop/delete/batch` and auto-append a line
to the local task ledger after every successful operation.

Rationale: a ledger that depends on someone remembering to update it will drift.
The wrapper keeps human/agent curation for intent (the table) but guarantees the
"what actually happened, when" event log records itself.

Usage:
  python job_ops.py create  --name my-job --workspace W --project P ...   # forwards to `inspire job create`
  python job_ops.py stop    my-job --workspace W                          # forwards to `inspire job stop`
  python job_ops.py delete  my-job --workspace W                          # forwards to `inspire job delete` (it prompts; pipe y if non-interactive)
  python job_ops.py batch   plan.json --workspace W                       # forwards to `inspire job batch`
  python job_ops.py --selftest                                             # append a sample event and exit

Behavior:
  - Exit code and output of `inspire` are passed through unchanged.
  - A line is appended only when the CLI exits 0 and the call was not --dry-run.
  - Ledger path: --ledger PATH, or INSPIRE_JOBS_LEDGER, or ~/.inspire/JOBS.md.
"""
import datetime
import os
import subprocess
import sys

INSPIRE = os.path.join(os.path.expanduser("~"), ".local", "bin", "inspire.exe")
if not os.path.exists(INSPIRE):
    INSPIRE = "inspire"

SUBCOMMANDS = ("create", "stop", "delete", "batch")


def ledger_path(cli_args):
    if "--ledger" in cli_args:
        i = cli_args.index("--ledger")
        return cli_args[i + 1]
    return os.environ.get("INSPIRE_JOBS_LEDGER") or os.path.join(
        os.path.expanduser("~"), ".inspire", "JOBS.md")


def pick(cli_args, flag):
    if flag in cli_args:
        i = cli_args.index(flag)
        if i + 1 < len(cli_args):
            return cli_args[i + 1]
    return ""


def positional(cli_args):
    for a in cli_args:
        if not a.startswith("-"):
            return a
    return ""


def append_event(path, line):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    header = "## Event Log"
    body = ""
    if os.path.exists(path):
        body = open(path, encoding="utf-8").read()
    if header not in body:
        body = body.rstrip("\n") + f"\n\n{header}\n(automatic, appended by job_ops.py)\n"
    body = body.rstrip("\n") + f"\n{line}\n"
    open(path, "w", encoding="utf-8").write(body)


def event_line(action, args):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    name = pick(args, "--name") or positional(args)
    bits = [f"name={name}" if name else ""]
    for flag in ("--workspace", "--project", "--group", "--quota", "--priority", "--nodes"):
        v = pick(args, flag)
        if v:
            bits.append(f"{flag.lstrip('-')}={v}")
    return f"- {ts} {action.upper()} " + " ".join(b for b in bits if b)


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        sys.exit(0)
    if argv[0] == "--selftest":
        append_event(ledger_path(argv), event_line("selftest", ["--name", "sample"]))
        print("selftest event appended to", ledger_path(argv))
        return
    action = argv[0]
    if action not in SUBCOMMANDS:
        print(f"unsupported action: {action}; expected one of {SUBCOMMANDS}")
        sys.exit(2)
    args = argv[1:]
    cmd = [INSPIRE, "job", action] + args
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.stdout:
        sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write(r.stderr)
    combined = (r.stdout or "") + (r.stderr or "")
    # The CLI can exit 0 while printing "Error: ..." — treat that as failure.
    failed = r.returncode != 0 or any(
        line.startswith("Error") or line.startswith("ERROR") for line in combined.splitlines())
    if not failed and "--dry-run" not in args:
        append_event(ledger_path(args), event_line(action, args))
    sys.exit(r.returncode if r.returncode else (1 if failed else 0))


if __name__ == "__main__":
    main()
