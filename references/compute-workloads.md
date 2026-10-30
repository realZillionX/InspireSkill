# Compute Workloads: Job, HPC, Ray, Serving & TensorBoard

Use this guide to choose between GPU Job, CPU HPC, Ray, and Serving, submit single or batch workloads, manage Serving API keys (`INF_API_KEY`), read TensorBoard curves, and observe running workloads. For quotas and availability, see [`resources.md`](resources.md); for images, see [`image.md`](image.md); for the Model Registry, see [`model.md`](model.md); for official datasets, see [`dataset.md`](dataset.md).

## 1. Choosing the Right Workload

| Goal | Workload | When to Use |
| --- | --- | --- |
| Background GPU training, batch inference, or worker pools | `inspire job` | Fixed GPU scale that runs to completion |
| CPU Slurm batch processing | `inspire hpc` | Fixed CPU scale for preprocessing, evaluation, and data pipelines |
| Elastic workers, long-lived daemons, or streaming | `inspire ray` | Requires a Ray Head, Driver, and autoscaling Worker Groups |
| Online HTTP model serving | `inspire serving` | Deploys a registered model version behind an HTTP endpoint |

Do not use Ray for fixed-size GPU training, do not use Notebooks for long unattended CPU batch runs, and do not use Serving for batch jobs.

## 2. Pre-Submission Checklist

Before submitting any workload:
1. **Workspace:** Use `CPU资源空间` for CPU HPC and CPU Ray; use `分布式训练空间` for GPU Jobs and Serving.
2. **Quota & Capacity:** Pick a valid `gpu,cpu,mem` row from `<workload> quota` and verify live capacity with `inspire resources availability` (if full, the workload stays `PENDING` with `FailedScheduling` events).
3. **Image:** Confirm the image is `READY` and already contains all required dependencies (do not run public internet downloads in offline GPU workspaces).
4. **Storage & Datasets:** Verify shared storage paths (`/inspire/...`). `job create` and `hpc create` can mount read-only official datasets via `--dataset <name>:<version>` (unsupported on `ray` and `serving`). `job`, `hpc`, `ray`, and `serving` can also mount the project public directory read-only (`--public-path-readonly`) to prevent accidental writes.
5. **Dry-run:** Run `<workload> create --dry-run` (or `<workload> batch <file> --dry-run`) to inspect the resolved plan before submitting.

## 3. GPU Job (`inspire job`)

`inspire job` runs single-node and multi-node GPU workloads (distributed training, batch inference, or concurrent worker pools):

- **Multi-node Jobs require full 8-GPU nodes:** When `--nodes n` (`n > 1`) is set, `--quota` must be an 8-GPU full-node spec, giving `n × 8` GPUs total. Partial-node multi-node jobs (`n × 2`, `n × 4`, `n × 6`) are rejected by the platform. Single-node jobs (`--nodes 1`) can use any valid GPU count.
- **Working directory:** The CLI does not prepend `cd` to your launch command. Explicitly `cd /inspire/...` in `--command` if your script expects a specific directory.
- **Shared memory (`--shm-size <GiB>`):** Configures per-instance `/dev/shm` (IPC memory for PyTorch DataLoader workers and NCCL) and cannot exceed the `mem` in `--quota gpu,cpu,mem`.
- **Environment variables:** Passed via repeatable `-e KEY=VALUE` flags and injected by the platform (the CLI echoes only variable names, never values).
- **Container retention:** Containers are released immediately upon completion by default. Set `--keep-after-success HOURS` or `--keep-after-failure HOURS` at creation time to hold the container in a retained state after exit so you can inspect it with `job shell`. These options apply to Jobs; HPC uses `--keep-after-finish HOURS`.
- **Notifications & fault tolerance:** Status notifications (`--enable-notification`, sent to the user's bound Feishu account) and automatic fault-tolerance retries are off by default. Account-level defaults for `shm`, notifications, and fault tolerance can be set under `[job]` in the account `config.toml` or via environment variables; scheduling parameters (`workspace`, `project`, `group`, `quota`, `image`) must always be passed explicitly.
- **Node placement:** Use repeatable `--exclude-node <name>` to avoid bad hosts, or `--specified-node <name>` to pin a job to specific nodes in the Compute Group (cannot overlap). `--specified-node` checks the Workspace's live `train_enable_specified_nodes` capability before submitting.
- **Idle reclamation & priority:** Workspaces reclaim running Jobs whose GPU utilization stays below policy thresholds (`inspire resources policy --workspace <name>`). Check row-level priority restrictions in `job quota` (`Priority` column: `low` requires `--priority 1`; see [`resources.md`](resources.md#4-quotas-priorities--budgets)).

## 4. CPU HPC (`inspire hpc`)

HPC has a two-layer resource model:
1. **Node layer (`--quota`, `--instance-count`):** Hardware allocated per node (CPUs, memory) and number of nodes in the dedicated Slurm cluster.
2. **Slurm layer (`--number-of-tasks`, `--cpus-per-task`, `--memory-per-cpu`):** How Slurm subdivides those nodes into tasks.

### Pre-Flight Validation & Resource Boundaries

- **Client-side validation:** The platform does not cross-check the Slurm layer against the node layer. `hpc create` blocks three invalid combinations before submission:
  1. `--cpus-per-task` exceeding a single node's CPUs (fails silently after launch).
  2. Per-node task memory (`tasks_on_node × cpus_per_task × memory_per_cpu`) exceeding a single node's memory (fails silently after launch).
  3. Total requested task CPUs (`number_of_tasks × cpus_per-task`) exceeding total allocated cluster CPUs (causes the job to report `RUNNING` while Slurm steps sit queued forever until killed by the Workspace runtime limit).
  When `--cpus-per-task` or `--memory-per-cpu` are omitted, the CLI derives safe values from the node quota and task count.
- **Memory is specified per CPU (`--memory-per-cpu`):** Never use the web console's "memory per node" field — its script generator still emits an empty `#SBATCH --mem-per-cpu=` line, causing every job to fail.
- **Respect cgroup limits, not host `nproc` / `free`:** Inside the container, `nproc` and `free` may report the physical host (e.g., 64 cores), while actual limits are enforced by the Pod cgroup (`cpu.max` and `memory.max` equal to `--quota`). Always size worker pools and buffers from `SLURM_CPUS_PER_TASK` and the `--quota` memory rather than auto-detecting host cores, and leave headroom for `slurmd` and the Python interpreter to avoid sudden cgroup OOM `SIGKILL`s.
- **Always launch work with `srun`:** The entrypoint (`-c`) is the Slurm script body (do not include `#!/bin/bash` or `#SBATCH` headers). If you omit `srun`, `sbatch` runs your command only on the first node, leaves all other nodes idle, and still reports `SUCCEEDED` with `Steps: 0/0`. Check `Steps` in `hpc status` (`1/1` vs `0/0`) to confirm Slurm steps actually ran.
- **Image & retention:** The selected image must include a working Slurm runtime (the platform image catalog does not filter for Slurm support). With default retention, HPC jobs briefly enter `SUCCEEDED_RETAINING` or `FAILED_RETAINING` after finishing; wait ~1 minute for the job to reach a terminal state before running `hpc delete` (`hpc stop` does not clear the retaining state). Set `--keep-after-finish HOURS` at creation time to retain the containers longer for inspection.

## 5. Ray (`inspire ray`)

Use Ray only when your workload genuinely needs autoscaling Worker Groups, long-running actors, heterogeneous workers, or streaming pipelines. A Ray cluster stays alive and consumes quota until the Driver exits or you run `ray stop`.

- **Requirements:** The image must contain the Ray runtime, and the Compute Group must support Ray (only groups supporting Ray appear in `ray quota`).
- **Autoscaling history (`ray scaling`):** Lists every replica count change (`initialized`, `scale_up`, `scale_down`) per Worker Group (`--group` filters to a specific role). An empty history means the group stayed at its initial replica count the entire time.
- **Restarting stopped clusters (`ray start`):** Stopped Ray jobs retain their Head, Worker Group, and Driver configuration and can be restarted in place with `ray start`. Because the platform API sometimes returns HTTP 200 without actually transitioning the cluster out of `STOPPED`, `ray start` polls until the status leaves `STOPPED` and reports an error if it does not move. Jobs that never reached `RUNNING` should be recreated rather than restarted.

## 6. Serving (`inspire serving`)

Serving deploys a registered model version (from `inspire model`) as an online HTTP service:

1. **Preparation:** Confirm the model version is ready (`model status`), check the minimum hardware spec (`model deploy-config`), pick a Serving quota (`serving quota` / `serving configs`), and verify that the image contains the serving framework.
2. **Lifecycle:** Create with `serving create` or `serving batch`; observe with `serving list|status|events|instances|logs|metrics|api-metrics`; control with `serving stop|start|scale|rollback|delete`.
3. **Scaling & rollback:**
   - `serving scale <name> --replicas N` adjusts replica count in place (each replica consumes a full quota unit; check `resources availability` before scaling up).
   - `serving versions` lists deployment configuration revisions, and `serving rollback --version V` redeploys a previous revision (replacing running replicas).
   - **`serving scale-history`** records every manual or automatic replica count change over time. If latency or throughput shifts without a new deployment revision in `versions`, check `scale-history` and align it with `api-metrics`.
4. **Resource metrics vs. API traffic metrics:**
   - `serving metrics` shows container GPU, CPU, and memory usage.
   - `serving api-metrics` shows request QPS, success/failure rates, latency, and token throughput — the only way to distinguish "nobody is calling the service" from "every request is failing".
5. **Startup failures:** Check `serving events` (which merges deployment-level and instance-level events by default). Readiness probe failures appear only as instance-level `Unhealthy` events while deployment-level events merely show `GroupsProgressing` or `Pending`.

### Calling Endpoints & Managing API Keys (`INF_API_KEY`)

- **Inspecting endpoints:** `serving list` and `serving status` display the platform-assigned endpoint URL (note that a URL can remain visible while the service is `STOPPED`). Run `inspire serving api <name> --workspace <workspace>` to view the full endpoint, service type, and invocation details (`--format curl` prints a ready-to-use `curl` template without sending a request or fetching secret keys; `--affinity-key <session-id>` adds the `x-inspire-inference-key` header for best-effort node affinity).
  - `CUSTOM` deployments expose whatever HTTP path, method, and payload your container listens on (the CLI prints a base root-URL `curl` template without assuming OpenAI compatibility).
  - Platform `EXCLUSIVE` / `SERVERLESS` deployments use `/v1` base URLs and `/v1/chat/completions`.
- **Managing API keys (`inspire account api-key`):** Inference API keys belong to the selected account and are sent by callers as `Authorization: Bearer $INF_API_KEY` (they are not passed to `serving create` or stored inside the Serving container).
  - `account api-key list` shows key names and creation timestamps.
  - `account api-key create --name <name>` creates a new key; `account api-key delete <name>` revokes it (check consumers before deleting).
  - Export or inject a key without exposing it in normal CLI output:

```bash
# Write a new .env file (0600 on POSIX, current-user-only ACL on Windows; never overwrites existing files)
inspire account api-key export my-key --format dotenv --output .env

# Capture into the current Bash / Zsh shell environment
INF_API_KEY="$(inspire account api-key export my-key --stdout)" && export INF_API_KEY

# Inject directly into a child process without printing the key
inspire account api-key run my-key -- python client.py
```

In PowerShell:

```powershell
$key = inspire account api-key export my-key --stdout
if ($LASTEXITCODE -eq 0) { $env:INF_API_KEY = $key }
Remove-Variable key
```

`export` supports `--format raw|dotenv|sh|powershell` and `--env-name <VAR>` with either `--output <file>` or `--stdout` (`--stdout` cannot be combined with `--json`). Inference API keys authenticate calls to Serving endpoints and applications; they are separate from user-center object-storage AccessKeys and SSH public keys.

## 7. Batch Submission (`<workload> batch`)

To submit a sweep of `job`, `hpc`, `notebook`, `ray`, or `serving` workloads that vary by name, command, or arguments, use `<workload> batch <file.json|file.toml>` instead of shell loops:
- Top-level `defaults` and `matrix` expand variants, while every expanded item must explicitly include `workspace`, `project`, `group`, `quota`, `image`, and workload-required fields (`profile` / `profiles` keys are rejected).
- Batch items accept the fields listed by each `<workload> batch --help`; supported fields vary by workload and can differ from `<workload> create`.
- Always run `<workload> batch <file> --dry-run` first to validate the expanded items and dataset mounts before creating anything.
- Within a single `job`, `hpc`, or `notebook` batch invocation, live quota priority menus, project catalogs, image catalogs, and successful `(workspace, dataset, version)` validations are fetched once per Workspace and reused across items in that run.

## 8. Observation & Debugging Loop

| Command | Primary Question Answered |
| --- | --- |
| `events` | Why is the workload queued, failing to schedule, or failing to start containers? (Merges controller and pod events by default; use `--instance` or `--workload-level` to narrow.) |
| `logs` | What did the application print? (Aggregates all instances by default; use `--instance` to filter. For Notebooks, use `exec` / `shell`.) |
| `metrics` | Are GPUs, CPUs, memory, and network actually active and balanced across Pods/replicas? (Queried one workload at a time.) |
| `instances` | Which Pods/roles are running, and which physical `Node` is each rank placed on? |
| `status` | Platform status, effective priority, assigned `Nodes`, and mounted datasets. |
| `job command` | Exact launch command used when the Job was submitted. |
| `shell` | Interactive PTY inside a running container (`exit` to quit, `Ctrl+]` to detach). Defaults to `launcher` on HPC, `head` on Ray, first running replica on Serving, or `--instance <label>`. |
| `tensorboard scalars` | Training loss and evaluation curves written to shared storage (see Section 9). |

### Multi-Workload Queries & Waiting

- **Batch status & events:** `job status`, `hpc status`, and `job events` accept multiple workload names in a single command (fetched in chunks of 20). Unresolved names (typos, deleted jobs, or ambiguous names) are listed under `Unresolved:` with a non-zero exit code without aborting the resolved items.
- **Cross-workspace search:** When you do not know which Workspace a Job is in, run `inspire job list --workspace all` (queries up to 8 Workspaces concurrently in round-robin pages).
- **Waiting for completion:** Only wait on workloads in non-terminal states (`PENDING`, `QUEUING`, `RUNNING`). Stopped Jobs (`CANCELLED`) and HPC jobs cannot be restarted; `start` exists on Notebook, Ray, Serving, and TensorBoard. `job wait` and `job logs --follow` exit automatically when the Job reaches a terminal state; `events --follow` and `job list --watch` stream indefinitely until interrupted — do not use them as completion barriers.

## 9. TensorBoard (`inspire tensorboard`)

TensorBoard is a standalone platform resource (`inspire tensorboard create|list|status|start|stop|delete|tags|scalars`):
- **Fixed spec (1 CPU / 2 GiB):** No `--quota` or `--image` is needed. You only choose `--workspace`, `--project`, `--group` (must be a Compute Group that declares `tensorboard` support), and `--summary-path <remote-dir>`.
- **`--job <name>` is optional metadata:** Associates the board with a training Job in the UI but does not infer `--summary-path`.
- **Auto-stop:** Defaults to 24 hours (up to `--auto-stop-hours 72`). Stopped boards can be restarted with `tensorboard start` without re-entering paths. Deleting a board requires `tensorboard stop` first and never deletes event files on shared storage.
- **Reading curves without a browser (`tags` & `scalars`):**
  - `inspire tensorboard tags <name> --workspace <workspace>` lists all runs and scalar tag names. (An empty tag list means either no event files exist yet or `--summary-path` is wrong.)
  - `inspire tensorboard scalars <name> --workspace <workspace>` returns start/end values, step ranges, and min/max values for each curve (sorted by `step`, not file order), plus the last `N` `(step, value)` points when `--points N` is passed.

## 10. Common Failure Patterns

| Symptom | Likely Cause & Next Step |
| --- | --- |
| Stuck in `PENDING` | Insufficient live capacity, low priority, or node filter mismatch → check `events` and `resources availability` |
| Stuck in `CREATING` | Image pull failure or node initialization delay → check pod-level `events` |
| Some `instances` stuck in `Pending` | Gang/multi-node or multi-replica scheduling shortage → check `events` and `Free Nodes` / `High Pri Nodes` |
| `RUNNING` with empty `logs` | Buffered stdout (`python -u` missing), logs written to a file instead of stdout, or entrypoint hung before printing → check `metrics` and `shell` |
| `FAILED` with no Python traceback | Container OOM (`SIGKILL`), GPU OOM, node eviction, or controller error → check `events` and `resources node-events <node>` |
| HPC `SUCCEEDED` with `Steps: 0/0` | Entrypoint omitted `srun`, so commands only ran on the first node → add explicit `srun` |
| HPC stuck `RUNNING` with `Steps: -/N` | Slurm task CPU/memory request exceeds allocated nodes → `hpc stop` and resubmit with matching quota |
| HPC log cuts off mid-run with no error | Hit Pod cgroup memory ceiling (`--quota` memory) or spawned too many workers from host `nproc` → size workers from `SLURM_CPUS_PER_TASK` |
| Background command never returns | Using `events --follow` / `list --watch`, which stream until interrupted → check `status` first and use `job wait` for Job completion |
