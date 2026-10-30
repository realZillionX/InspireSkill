# Resources & Scheduling

Use this guide when selecting Workspaces, Projects, Compute Groups, `--quota` specs, priorities, and images, or when inspecting live cluster availability, usage attribution, schedule policies, and local caches. For shared storage paths, see [`paths.md`](paths.md); for network boundaries and internal mirrors, see [`internal-sources.md`](internal-sources.md).

## 1. Three Categories of Identifiers

| Category | What It Controls | Typical Fields |
| --- | --- | --- |
| Scheduling | Where a workload runs, its hardware allocation, and its container image | `workspace`, `project`, `group`, `quota`, `image` |
| Remote Paths | Where code, datasets, weights, checkpoints, and outputs are stored | Absolute `/inspire/<tier>/project/...` paths |
| Object Names | Which platform object you are inspecting, connecting to, or deleting | Name of the Notebook, Job, HPC job, Ray job, Serving deployment, or TensorBoard |

Scheduling parameters have no implicit defaults. Pass all five fields explicitly whenever creating a workload.

## 2. Workspaces

`CPU资源空间` and `分布式训练空间` are the two default public Workspaces available to all users:

| Workspace | Primary Role |
| --- | --- |
| `CPU资源空间` | CPU Notebooks, internet downloads, dependency preparation, CPU HPC jobs, CPU Ray jobs |
| `分布式训练空间` | GPU Notebooks, GPU Jobs, multi-node training, Serving deployments, GPU metrics observation |

Other Workspaces (project-dedicated workspaces, specialized accelerator partitions, etc.) must be explicitly specified by the user. Never select them automatically just because they appear in a listing.

## 3. Querying Live Resource State

Always query live platform state before submitting workloads:

1. Check visible Workspaces, Projects, and Compute Groups for the active account.
2. Query `<workload> quota --workspace <name>` to list valid `gpu,cpu,mem` specs supported by each Compute Group for that workload type.
3. Query `inspire resources availability --workspace <name>` to check both guaranteed quota balances and physical 8-GPU full-node capacity.
4. Use `--group <keyword>` (case-insensitive substring match) to narrow resource queries to relevant Compute Groups, then copy the **full** Compute Group name into `--group` when creating a workload.

**Single-workspace scope:** `resources availability`, `resources policy`, `resources usage`, `<workload> quota`, and `serving configs` require a single `--workspace` and do not accept `all`, because quotas, capacity, reclaim rules, and usage are defined per Workspace. `resources node-events` takes node names and has no `--workspace` option. Only cross-workspace discovery commands (`<workload> list` and `account permissions`) accept `--workspace all`.

### `resources availability`: Quota Balance vs. Full Nodes

| Column / Field | Meaning |
| --- | --- |
| `Available` | Remaining guaranteed GPU quota allocated to this Workspace in the Compute Group (`gpu_total - gpu_used`). Can be **negative** when fair-share scheduling allows low-priority jobs to borrow beyond guaranteed quota. |
| `High Pri GPU` (`Available + low_priority_gpus`) | GPUs available to high-priority workloads after preempting low-priority tasks (`low_priority_gpus` in `--json`). Can also be negative if guaranteed quota is heavily overdrawn. |
| `Free Nodes` | Physical 8-GPU nodes that are completely idle right now (`READY`, zero tasks, zero used GPUs, uncordoned, not in maintenance or fault pools). |
| `High Pri Nodes` | Physical 8-GPU nodes that are either completely idle (`Free Nodes`) or occupied exclusively by preemptible low-priority tasks. |
| `Idle GPUs` | Strictly `Free Nodes × 8` (physical idle full-node GPUs, independent of Workspace quota balance). |

Multi-node GPU Jobs can only be submitted in full 8-GPU node units (`--nodes n` where each instance uses an 8-GPU quota). Check `Free Nodes` and `High Pri Nodes` before submitting multi-node jobs.

Zero-guarantee groups (`gpu_total == 0`) can still contain active GPU nodes and workloads; the CLI classifies GPU groups using live usage, GPU model metadata, and node dimensions rather than relying on `gpu_total > 0`.

### `resources policy`: Auto-Stop & Runtime Limits

`inspire resources policy --workspace <name>` shows how long a workload can keep its resources:
- **`Reclaim`** — whether the scheduler automatically reclaims idle instances.
- **`Idle Rule`** — GPU utilization and duration thresholds that trigger reclamation (based on resource metrics, not active SSH/terminal sessions). Serving rules may be broken down by GPU count range.
- **`Time Limit`** — hard runtime ceiling (`max` runtime for Jobs, `daily` shutdown time for Notebooks).
- A `-` means the Workspace does not declare an explicit rule for that workload (which does not guarantee unlimited runtime). Check `resources policy` before leaving workloads running overnight.

### `resources usage`: Usage Attribution & Preemptible GPUs

`inspire resources usage --workspace <name>` attributes active resources using `TaskDimension` records:
- Defaults to `Project → User` aggregation. Pass `--details` to expand to individual tasks, or filter with `--project`, `--user`, `--task`, or `--group <keyword>`.
- **`--mine`** queries `UserDimension` filtered to the current user's pre-aggregated project rows (cannot be combined with `--group`, since user-dimension rows are not broken down by compute group).
- **`Reclaimable`** shows how many GPUs are held by tasks submitted at preemptible low priority (`< 4` in fair-share Workspaces, `≤ 3` elsewhere; `-` if the Workspace priority contract is unavailable).
- `Reclaimable` in `usage` and `low_priority_gpus` in `availability` are computed differently (client-side task aggregation vs. platform-side group accounting) and may differ slightly across snapshots. Use `availability` to judge how many GPUs you can claim, and `usage` to see which projects or users hold them.
- GPU utilization rate is omitted from the human table (still present as `gpu_usage_rate` in `--json`).

### Local Cache (`inspire cache`)

The CLI maintains a per-account SQLite cache (`resource-index.sqlite3`) solely to speed up name-to-handle and quota resolution. Live commands (`list`, `status`, `events`, `metrics`, `resources *`) always query the platform directly.

Manage the cache with `inspire cache status | refresh | clear`:
- All three accept repeatable `--resource <kind>` filters across four categories:
  - Platform catalogs: `workspace`, `project`, `compute-group`, `image`, `model`
  - Workloads: `notebook`, `job`, `hpc`, `ray`, `serving`
  - Quotas: `quota-<workload>`
  - Notebook GPU probes: `notebook-gpu` (supports `status` and `clear` only; populated on demand per Compute Group)
- **TTL:** 1 day for workload names and account/workspace/project/compute-group/model catalogs; 7 days for `image` and `quota-<workload>`.
- **On-demand population:** Normal commands populate cache scopes on demand and immediately update or tombstone entries when creating or deleting resources via the CLI. They never trigger background full-account scans.
- **`inspire cache refresh` requires an explicit target** (`--resource`, `--workspace`, or `--name`). Only run it when upstream catalogs changed outside the CLI (e.g., an admin modified compute groups or you deleted an image in the web UI). Workload history scans are capped at 5,000 rows.
- **`cache status` states:**
  - `partial`: populated by targeted name lookups rather than a full scan (normal, not an error).
  - `empty`: a full refresh succeeded within TTL but returned zero items across the entire resource kind. Run `cache refresh --resource <kind>` to verify whether the catalog is genuinely empty.
  - `error` / `incomplete`: one or more fan-out requests (e.g., per-Compute-Group quota fetches) failed due to throttling or 5xx errors; previously cached rows are preserved for failed groups while the error summary is recorded.
- `cache clear` only clears name/quota/GPU-probe caches; it does not touch login sessions, SSH connections, or proxy settings.

## 4. Quotas, Priorities & Budgets

### Quota Triples (`--quota` / `-q`)

`--quota` takes a `gpu,cpu,mem` triple where `mem` is regular instance memory in GiB (GPU model is determined by Workspace + Compute Group).
- For GPU Jobs, `--shm-size <GiB>` configures `/dev/shm` (IPC shared memory) per instance and cannot exceed the quota's `mem`. Verify resolved values with `job create --dry-run`.
- If the same `gpu,cpu,mem` triple exists in multiple Compute Groups, disambiguate by passing the full Compute Group name in `--group`.

### Priority Contracts (`--priority`)

The CLI automatically selects the priority scale for the target Workspace:

| Workspace Type | Allowed `--priority` Values | Default |
| --- | --- | --- |
| Fair-share Workspace | `1` (`LOW`, preemptible) or `4` (`HIGH`, non-preemptible) | `4` |
| Standard Workspace | `1–10` (`≤ 3` treated as low priority) | `10` |

Project-level policies may cap your effective priority below the requested value. Always check `status` and `events` after submission to confirm the effective priority.

### Per-Quota Priority Restrictions (`Priority` Column)

Individual quota rows can restrict which priorities they accept, shown in the `Priority` column of `<workload> quota` (`priority` and `allowed_priority_levels` in `--json`):

| `Priority` Value | Meaning |
| --- | --- |
| `any` (`allowed_priority_levels: []`) | No row-level restriction; governed only by Workspace and Project policies |
| `low` (`allowed_priority_levels: ["low"]`) | Only accepts low priority (`--priority 1` in fair-share Workspaces; preemptible) |
| `unknown` (`allowed_priority_levels: null`) | Schedule config could not be read; pre-flight check does not block submission |

`job create`, `notebook create`, and `serving create` validate `--priority` against the selected quota row before sending the request. Restrictions apply per instance quota row (for multi-node Jobs, `--nodes n` requires an 8-GPU full-node quota row per instance).

### Point Cost (`Points/h`) & Project Budgets

- **`Points/h`** (`points_per_hour` in `--json`) in `<workload> quota` shows the live hourly point cost **per instance** (multiply by `--nodes` or `--replicas` for total hourly rate). CPU-only rows currently report `0`; `-` (`null`) means the platform did not return a price (not guaranteed free).
- **`inspire project list`** reports two distinct budget columns:
  - **`My Budget`** (`my_remaining_budget`): remaining points available to **your account** in that project (the limit that gates your next submission). Falls back to `Project Budget` when per-member quotas are not configured.
  - **`Project Budget`** (`project_remaining_budget`): remaining points shared across the entire project.
- **`inspire project detail <name>`** breaks down spending (`on training`, `on inference`, `on storage`, `on private workspace`).

## 5. Scheduling & Node Diagnostics

| Symptom | What to Check |
| --- | --- |
| `0 candidates` or `Quota Match Failed` | Mismatched Workspace, Compute Group, or `gpu,cpu,mem` triple; check `<workload> quota` |
| Stuck in `PENDING` | Insufficient live capacity, low priority, or node placement constraints; check `events` and `resources availability` |
| `RUNNING` with no progress | Check `metrics` for GPU/CPU/I/O activity, then inspect `logs` and shared storage outputs |
| Single lagging worker in multi-node job | Compare per-instance `metrics` and `instances` (to identify the rank and node), then check that worker's `logs` |
| Repeated failures on the same physical machine | Run `inspire resources node-events <node-name>` |
| Workload unexpectedly stopped | Check `events` for preemption, then check `resources policy` for idle reclamation or runtime limits |

### Node-Level Events (`resources node-events`)

Workload `events` only show controller and pod actions for that workload. To inspect hardware or kernel failures on the host machine itself (kernel OOM kills, `TaskHung`, Cordon/Uncordon, reboots, `NodeNotSchedulable`), run:

```bash
inspire resources node-events <node-name>...
```

Get `<node-name>` from `<workload> instances` or `<workload> status`. You can pass multiple nodes at once. The command fetches the latest 1,000 events in reverse chronological order and restores chronological order; use `--from` (`kubelet`, `kernel-monitor`, `node-controller`) for server-side component filtering and `--type` / `--reason` for client-side filtering.
