# Repeated & Concurrent Workload Operations

Use this guide when operating workloads across sessions or sharing a Project with other users. Query platform state before resuming work; keep durable assets in [`INSPIRE.md`](assets.md).

## Task Handoffs & Ownership

- For a task that spans sessions, keep a temporary run plan with the account alias, Workspace, Project, workload names, purpose, output paths, and completion or release condition. Keep credentials and live status out of it, and discard completed entries once outputs are verified and temporary resources are cleaned up.
- At handoff, compare the plan with live workload lists and inspect `status`, `events`, and outputs. A missing list entry alone does not establish completion: check the account, Workspace, filters, and pagination first.
- Scope workload names with a run-specific suffix. Names are not unique; resolve ambiguous names using `--pick` before acting.
- Confirm a workload belongs to the current task before stopping or deleting it. Other users and concurrent sessions may have valid work in the same Project. Investigate unexplained workloads without assuming they are disposable.

## Compute Group Selection

- Compare eligible Compute Groups using one fresh `resources availability` snapshot. Match the GPU model, per-instance quota, and accepted priorities using `<workload> quota`; use live policy data rather than inferring eligibility from a group's name.
- For partial-node workloads, inspect the guaranteed balance and the capacity available at the requested priority. For full-node workloads, compare the requested node count with `Free Nodes` or `High Pri Nodes` as appropriate. **`High Pri Nodes` already includes `Free Nodes`; do not add them.** See [`resources.md`](resources.md#resources-availability-quota-balance-vs-full-nodes).
- Availability is a scheduling snapshot, not a reservation. If a workload remains unschedulable, inspect its events and refresh capacity before changing its specification or Compute Group. Stop the superseded workload and confirm its terminal state before submitting a replacement.

## Shared Quota & Submission Waves

- Check usage across the parent Project, child Projects, and all members, including other sessions of the same account. A personal workload list does not show the whole shared pool.
- Use `resources usage --workspace <name> --details --all` to inspect the relevant Projects and users. This reports consumption, not the scheduler's configured project GPU cap. When a scheduler event reports `used` and `quota`, use that limit to resize the wave; do not infer headroom from idle GPUs or remaining points.
- Submit useful work with a defined completion condition. Release temporary allocations promptly after their consumer finishes; idle quota-holding jobs consume the same pool as collaborators' work.
- Follow the batch validation, pacing, and duplicate-name guidance in [`compute-workloads.md`](compute-workloads.md).
