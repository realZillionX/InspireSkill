# Repeated & Concurrent Workload Operations

Use this guide when operating workloads across sessions or sharing a Project with collaborators. Query live platform state before resuming work, and keep durable asset records in [`INSPIRE.md`](assets.md).

## Task Handoffs & Ownership

- For multi-session work, maintain a temporary run plan listing the account alias, Workspace, Project, workload names, purpose, output paths, and completion or release criteria. Keep credentials and live status out of the plan, and remove completed entries once outputs are verified and temporary resources are deleted.
- At handoff, reconcile the plan against live workload lists and inspect `status`, `events`, and shared-storage outputs. A missing list entry alone does not prove completion — verify the account, Workspace, status/keyword filters, and pagination first.
- Scope workload names with a run-specific suffix. Job, HPC, Ray, Serving, and TensorBoard names are not unique (only `notebook create` enforces per-workspace uniqueness); resolve ambiguous names explicitly with `--pick` before acting.
- Confirm that a workload belongs to the current task before stopping or deleting it. Collaborators and concurrent sessions often run valid workloads in the same Project.

## Compute Group Selection

- Compare candidate Compute Groups using a fresh `inspire resources availability --workspace <name> --all` snapshot. Match the GPU model, per-instance quota, and accepted priorities via `<workload> quota --workspace <name> --all`; rely on live policy data rather than inferring eligibility from a group's name.
- For partial-node workloads, check both the guaranteed balance (`Available`) and capacity at the requested priority (`High Pri GPU`). For multi-node full-node workloads, compare the requested node count against `Free Nodes` (`full_free_nodes` in JSON) or `High Pri Nodes` (`high_priority_free_nodes` in JSON). **`high_priority_free_nodes` already includes `full_free_nodes` — never add them together.** General `free_nodes` counts idle nodes of any GPU count, and `gpus_per_node` reports the largest node size in the group. See [`resources.md`](resources.md#resources-availability-quota-balance-vs-full-nodes).
- Availability is a point-in-time snapshot, not a reservation. If a workload stays unschedulable, inspect `events` and re-check capacity before changing its spec or Compute Group. Stop any superseded workload and confirm its terminal state before submitting a replacement.

## Shared Quota & Submission Waves

- Check usage across the parent Project, child Projects, and all members — including other sessions under the same account. A personal workload list never shows the full shared pool.
- Run `inspire resources usage --workspace <name> --details --all` to inspect active consumption across Projects and users. Usage shows current consumption rather than the configured project GPU cap; when a scheduler event reports `used` and `quota` (e.g., `Parent&sub project total quota insufficient`), resize the wave from those numbers instead of guessing headroom from idle GPUs or remaining points.
- Every submitted workload should have a clear completion condition. Release temporary allocations promptly once their downstream consumer finishes so idle jobs do not block collaborators.
- Follow the batch validation, wave sizing, and duplicate-name rules in [`compute-workloads.md`](compute-workloads.md).
