# Agent Operations Discipline

For agents that operate the platform repeatedly across sessions, or in parallel with other agents and users. These rules complement the platform contracts in the other references: they exist because field sessions keep reproducing the same failure classes (idle GPUs while submissions trickle out, orphaned workloads nobody remembers, waves rejected by a shared quota that was never checked).

## Cross-Session Task Ledger

The CLI intentionally keeps no local task registry, and live queries are the source of truth for *state*. But an agent that forgets which workloads it created will leak running jobs for days. Keep an optional local ledger (e.g. `~/.inspire/JOBS.md`, never committed to Git):

- Record **intent only** at creation time: workload name, project, group, spec, purpose, and a release condition. Never record live status in the ledger — status is always re-read from the platform.
- Reconcile at the start of every session: read the ledger, run `job list` / `notebook list` for the workspaces in use, and cross-check. Ledger entries with no live counterpart are finished (collect outputs, then stop/delete and archive the row); live workloads with no ledger entry are **unowned** — surface them to the user instead of silently ignoring them.
- Update the ledger immediately after every create/stop/delete. A workload that was never logged is functionally unowned, including your own. For automated loops, do not rely on remembering this: `scripts/job_ops.py` wraps `job create/stop/delete/batch`, forwards everything to the CLI unchanged, and appends one event line per successful operation — the curated table keeps intent and release conditions, the auto event log keeps "what actually happened, when".

## Zone Selection Before Submission

- Decide the channel first: fragment (1/2/4 GPUs per instance) or whole-node (multiples of 8, `--nodes n` requires an 8-GPU full-node quota per instance).
- Rank candidate compute groups from **one** `resources availability` snapshot instead of submitting to the first group you happen to list: fragments rank by high-priority-grabbable GPUs, whole nodes rank by `(Free Nodes + High Pri Nodes) × gpus_per_node`. Zones that only take whole-node workloads reject high-priority fragments — check the zone's rules before picking.
- If the top choice shows no scheduling progress (events all `Unschedulable`) for roughly 10 minutes, stop it and move to the runner-up group; do not re-submit into the same group repeatedly.
- Treat the zone-picker as part of the create path, not an optional helper: run `scripts/pick_zone.py` (or an equivalent ranking built from one `resources availability` snapshot) before **every** GPU `create`, single-job or batch. A submission that skipped a fresh ranking is a process error even when it happens to succeed — field sessions relapse to "first group I listed" within a day when this step is optional.
- A reference implementation of the ranking lives at `scripts/pick_zone.py`, and `scripts/job_ops.py` wraps `job create/stop/delete/batch` to auto-append ledger events (see below).

## Shared Project Quota & Concurrent Sessions

- Project running quota counts the parent project, all child projects, and every member — **including your own other agent sessions**. Two sessions of the same account compete for the same pool without knowing about each other; the ledger and a pre-wave `resources usage --project <name>` check are the coordination mechanisms.
- Before a large or multi-node wave, check live quota headroom and size the wave from it. If submissions are rejected with a quota error, read the `used`/`quota` numbers from the message and resize instead of retrying. See the pacing rules in `compute-workloads.md` §7.
